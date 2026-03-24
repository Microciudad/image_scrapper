"""Audio file scanner utilities."""

import logging
from pathlib import Path
from typing import Optional

from mutagen import File as MutagenFile
from mutagen.id3 import APIC

from ..domain.models import AudioFile

logger = logging.getLogger(__name__)


class AudioScanner:
    """Escaneador de archivos de audio para extraer metadatos."""
    
    def scan_file(self, file_path: Path) -> AudioFile:
        """Escanea un archivo de audio y extrae sus metadatos."""
        try:
            audio_file = MutagenFile(file_path)
            
            if audio_file is None:
                logger.debug(f"No se pudo leer el archivo de audio: {file_path}")
                return AudioFile(path=file_path)
            
            # Extraer metadatos básicos
            title = self._extract_title(audio_file)
            artist = self._extract_artist(audio_file)
            album = self._extract_album(audio_file)
            has_image = self._has_embedded_image(audio_file)
            
            return AudioFile(
                path=file_path,
                title=title,
                artist=artist,
                album=album,
                has_embedded_image=has_image
            )
            
        except Exception as e:
            # Log corrupted/invalid MP3 files at DEBUG level, not ERROR
            # These are expected for some files with corrupt headers or non-standard formats
            error_msg = str(e).lower()
            if 'sync' in error_msg or 'frame' in error_msg or 'synchsafe' in error_msg or 'header' in error_msg or 'corrupt' in error_msg or 'mpeg' in error_msg:
                logger.debug(f"Skipping corrupted audio file: {file_path.name} - {e}")
            else:
                logger.debug(f"Error scanning audio file {file_path}: {e}")
            return AudioFile(path=file_path)
    
    def _extract_title(self, audio_file) -> Optional[str]:
        """Extrae el título de la canción."""
        try:
            # Probar diferentes tags según el formato
            for tag in ['TIT2', 'TITLE', '\xa9nam']:
                if tag in audio_file:
                    value = audio_file[tag]
                    if isinstance(value, list) and value:
                        return str(value[0])
                    elif isinstance(value, str):
                        return value
            return None
        except Exception:
            return None
    
    def _extract_artist(self, audio_file) -> Optional[str]:
        """Extrae el artista."""
        try:
            # Probar diferentes tags según el formato
            for tag in ['TPE1', 'ARTIST', '\xa9ART']:
                if tag in audio_file:
                    value = audio_file[tag]
                    if isinstance(value, list) and value:
                        return str(value[0])
                    elif isinstance(value, str):
                        return value
            return None
        except Exception:
            return None
    
    def _extract_album(self, audio_file) -> Optional[str]:
        """Extrae el álbum."""
        try:
            # Probar diferentes tags según el formato
            for tag in ['TALB', 'ALBUM', '\xa9alb']:
                if tag in audio_file:
                    value = audio_file[tag]
                    if isinstance(value, list) and value:
                        return str(value[0])
                    elif isinstance(value, str):
                        return value
            return None
        except Exception:
            return None
    
    def _has_embedded_image(self, audio_file) -> bool:
        """Verifica si el archivo tiene una imagen embebida."""
        try:
            # Para MP3 (ID3)
            if hasattr(audio_file, 'tags') and audio_file.tags:
                for tag in audio_file.tags.values():
                    if isinstance(tag, APIC):
                        return True
            
            # Para FLAC
            if hasattr(audio_file, 'pictures') and audio_file.pictures:
                return True
            
            # Para MP4/M4A
            if 'covr' in audio_file:
                return bool(audio_file['covr'])
            
            # Para OGG
            if hasattr(audio_file, 'tags'):
                artwork_keys = ['METADATA_BLOCK_PICTURE', 'COVERART', 'ALBUMARTIST']
                for key in artwork_keys:
                    if key in audio_file.tags:
                        return True
            
            return False
            
        except Exception:
            return False
