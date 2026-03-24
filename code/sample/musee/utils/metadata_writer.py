"""Metadata writer utilities for embedding images in audio files."""

import logging
from io import BytesIO
from pathlib import Path
from typing import Optional, TYPE_CHECKING

from mutagen import File as MutagenFile
from mutagen.flac import FLAC, Picture
from mutagen.id3 import APIC, ID3, TXXX
from mutagen.mp4 import MP4, MP4Cover
from mutagen.oggvorbis import OggVorbis
from PIL import Image

if TYPE_CHECKING:
    from .image_marker import MuseeImageMarker

logger = logging.getLogger(__name__)


class MetadataWriter:
    """Escritor de metadatos para embeber imágenes en archivos de audio."""
    
    def embed_image(self, file_path: Path, image_data: bytes, 
                    marker: Optional['MuseeImageMarker'] = None) -> None:
        """Embebe una imagen en un archivo de audio."""
        try:
            # Validar y procesar imagen
            image = Image.open(BytesIO(image_data))
            
            # Convertir a RGB si es necesario
            if image.mode not in ('RGB', 'L'):
                image = image.convert('RGB')
            
            # Redimensionar si es muy grande (500x500 para metadatos)
            if image.width > 500 or image.height > 500:
                image.thumbnail((500, 500), Image.Resampling.LANCZOS)
            
            # Convertir de vuelta a bytes
            output = BytesIO()
            image.save(output, format='JPEG', quality=85)
            processed_image_data = output.getvalue()
            
            # Embeber según el formato del archivo
            extension = file_path.suffix.lower()
            
            if extension == '.mp3':
                self._embed_mp3(file_path, processed_image_data, marker)
            elif extension == '.flac':
                self._embed_flac(file_path, processed_image_data, marker)
            elif extension in ('.m4a', '.mp4'):
                self._embed_mp4(file_path, processed_image_data, marker)
            elif extension == '.ogg':
                self._embed_ogg(file_path, processed_image_data, marker)
            else:
                logger.warning(f"Formato no soportado para embeber imagen: {extension}")
                
        except Exception as e:
            error_msg = str(e).lower()
            # Check for common corruption errors
            if 'sync' in error_msg or 'frame' in error_msg or 'mpeg' in error_msg:
                logger.debug(f"Skipping corrupted audio file (invalid MPEG frame): {file_path}")
            else:
                logger.debug(f"Cannot embed image in {file_path}: {e}")
            raise
    
    def _embed_mp3(self, file_path: Path, image_data: bytes, 
                   marker: Optional['MuseeImageMarker'] = None) -> None:
        """Embebe imagen en archivo MP3."""
        try:
            audio_file = MutagenFile(file_path)
            
            if audio_file is None:
                logger.debug(f"Skipping corrupted or invalid MP3 file: {file_path}")
                raise ValueError(f"Cannot read MP3 file (corrupted tags?): {file_path}")
            
            # Agregar tags ID3 si no existen
            if audio_file.tags is None:
                audio_file.add_tags()
            
            # Eliminar imágenes existentes
            audio_file.tags.delall('APIC')
            
            # Agregar nueva imagen
            audio_file.tags.add(
                APIC(
                    encoding=3,  # UTF-8
                    mime='image/jpeg',
                    type=3,  # Cover (front)
                    desc='Cover',
                    data=image_data
                )
            )
            
            # Embeber marker si está disponible
            if marker:
                try:
                    marker_json = marker.to_json()
                    audio_file.tags.add(
                        TXXX(
                            encoding=3,  # UTF-8
                            desc='MUSEE_MARKER',
                            text=[marker_json]
                        )
                    )
                    logger.debug(f"MUSEE marker embedded in ID3 tags: {file_path}")
                except Exception as e:
                    logger.debug(f"Could not embed MUSEE marker in {file_path}: {e}")
                    # No-fatal, continúa sin marcador
            
            audio_file.save()
            logger.info(f"Image embedded in MP3: {file_path}")
            
        except Exception as e:
            error_msg = str(e)
            if 'sync' in error_msg.lower() or 'frame' in error_msg.lower() or 'mpeg' in error_msg.lower():
                logger.debug(f"Skipping corrupted MP3 (invalid MPEG frame): {file_path}")
            else:
                logger.debug(f"Cannot embed image in MP3 {file_path}: {e}")
            raise
    
    def _embed_flac(self, file_path: Path, image_data: bytes, 
                    marker: Optional['MuseeImageMarker'] = None) -> None:
        """Embebe imagen en archivo FLAC."""
        try:
            audio_file = FLAC(file_path)
            
            # Eliminar imágenes existentes
            audio_file.clear_pictures()
            
            # Crear nueva imagen
            picture = Picture()
            picture.type = 3  # Cover (front)
            picture.mime = 'image/jpeg'
            picture.desc = 'Cover'
            picture.data = image_data
            
            # Obtener dimensiones de la imagen
            image = Image.open(BytesIO(image_data))
            picture.width = image.width
            picture.height = image.height
            picture.depth = 24  # 24-bit color depth
            
            audio_file.add_picture(picture)
            
            # Embeber marker si está disponible
            if marker:
                try:
                    marker_json = marker.to_json()
                    audio_file['MUSEE_MARKER'] = [marker_json]
                    logger.debug(f"Marcador MUSEE embebido en Vorbis comment para: {file_path}")
                except Exception as e:
                    logger.warning(f"No se pudo embeber marcador MUSEE en {file_path}: {e}")
                    # No-fatal
            
            audio_file.save()
            
            logger.info(f"Imagen embebida en FLAC: {file_path}")
            
        except Exception as e:
            logger.error(f"Error embebiendo imagen en FLAC {file_path}: {e}")
            raise
    
    def _embed_mp4(self, file_path: Path, image_data: bytes, 
                   marker: Optional['MuseeImageMarker'] = None) -> None:
        """Embebe imagen en archivo MP4/M4A."""
        try:
            audio_file = MP4(file_path)
            
            # Agregar imagen
            cover = MP4Cover(image_data, imageformat=MP4Cover.FORMAT_JPEG)
            audio_file['covr'] = [cover]
            
            # Embeber marker si está disponible
            if marker:
                try:
                    marker_json = marker.to_json()
                    audio_file['com.apple.itunes.MUSEE_MARKER'] = marker_json
                    logger.debug(f"Marcador MUSEE embebido en iTunes atom para: {file_path}")
                except Exception as e:
                    logger.warning(f"No se pudo embeber marcador MUSEE en {file_path}: {e}")
                    # No-fatal
            
            audio_file.save()
            logger.info(f"Imagen embebida en MP4: {file_path}")
            
        except Exception as e:
            logger.error(f"Error embebiendo imagen en MP4 {file_path}: {e}")
            raise
    
    def _embed_ogg(self, file_path: Path, image_data: bytes, 
                   marker: Optional['MuseeImageMarker'] = None) -> None:
        """Embebe imagen en archivo OGG."""
        try:
            audio_file = OggVorbis(file_path)
            
            # Para OGG, codificar imagen en base64 y usar METADATA_BLOCK_PICTURE
            import base64
            
            # Crear estructura de Picture similar a FLAC
            picture = Picture()
            picture.type = 3  # Cover (front)
            picture.mime = 'image/jpeg'
            picture.desc = 'Cover'
            picture.data = image_data
            
            # Obtener dimensiones
            image = Image.open(BytesIO(image_data))
            picture.width = image.width
            picture.height = image.height
            picture.depth = 24
            
            # Codificar en base64
            picture_data = picture.write()
            encoded_data = base64.b64encode(picture_data).decode('ascii')
            
            # Eliminar imágenes existentes
            if 'METADATA_BLOCK_PICTURE' in audio_file:
                del audio_file['METADATA_BLOCK_PICTURE']
            
            # Agregar nueva imagen
            audio_file['METADATA_BLOCK_PICTURE'] = [encoded_data]
            
            # Embeber marker si está disponible
            if marker:
                try:
                    marker_json = marker.to_json()
                    audio_file['MUSEE_MARKER'] = [marker_json]
                    logger.debug(f"Marcador MUSEE embebido en Vorbis comment para: {file_path}")
                except Exception as e:
                    logger.warning(f"No se pudo embeber marcador MUSEE en {file_path}: {e}")
                    # No-fatal
            
            audio_file.save()
            logger.info(f"Imagen embebida en OGG: {file_path}")
            
        except Exception as e:
            logger.error(f"Error embebiendo imagen en OGG {file_path}: {e}")
            raise
