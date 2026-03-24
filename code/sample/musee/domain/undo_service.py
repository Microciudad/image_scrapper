"""Service for undoing MUSEE image downloads."""

import logging
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from mutagen import File as MutagenFile

from ..utils.image_marker import detect_musee_image, remove_image_marker

logger = logging.getLogger(__name__)


class UndoService:
    """Service for reverting MUSEE-downloaded images."""
    
    AUDIO_EXTENSIONS = {'.mp3', '.flac', '.m4a', '.mp4', '.ogg', '.wav'}
    FOLDER_IMAGE_NAME = 'folder.jpg'
    
    def revert_folder_images(self, root_path: Path, dry_run: bool = False) -> Dict[str, int]:
        """
        Revert all MUSEE-downloaded folder images.
        
        Args:
            root_path: Root directory to search
            dry_run: If True, don't delete, just report what would be deleted
            
        Returns:
            Dictionary with counts: {deleted: N, skipped: M, errors: K}
        """
        stats = {'deleted': 0, 'skipped': 0, 'errors': 0}
        
        try:
            # Find all folder.jpg files recursively
            folder_images = list(root_path.rglob(self.FOLDER_IMAGE_NAME))
            
            logger.info(f"Encontrados {len(folder_images)} archivos {self.FOLDER_IMAGE_NAME}")
            
            for image_path in folder_images:
                try:
                    # Check if image has MUSEE marker
                    is_musee, marker = detect_musee_image(image_path)
                    
                    if is_musee and marker:
                        logger.info(
                            f"Marcador MUSEE detectado en: {image_path}\n"
                            f"  - Fuente: {marker.source}\n"
                            f"  - Artista: {marker.artist}\n"
                            f"  - Album: {marker.album}\n"
                            f"  - Timestamp: {marker.timestamp}"
                        )
                        
                        if not dry_run:
                            try:
                                image_path.unlink()
                                logger.info(f"Imagen eliminada: {image_path}")
                                stats['deleted'] += 1
                            except Exception as e:
                                logger.error(f"Error eliminando {image_path}: {e}")
                                stats['errors'] += 1
                        else:
                            logger.info(f"[DRY RUN] Se eliminaría: {image_path}")
                            stats['deleted'] += 1
                    else:
                        logger.debug(f"Sin marcador MUSEE (imagen preexistente): {image_path}")
                        stats['skipped'] += 1
                        
                except Exception as e:
                    logger.error(f"Error procesando {image_path}: {e}")
                    stats['errors'] += 1
                    
        except Exception as e:
            logger.error(f"Error en revert_folder_images: {e}")
            stats['errors'] += 1
            
        return stats
    
    def revert_song_images(self, root_path: Path, dry_run: bool = False) -> Dict[str, int]:
        """
        Revert all MUSEE-downloaded song images from audio metadata.
        
        Args:
            root_path: Root directory to search
            dry_run: If True, don't modify, just report what would be reverted
            
        Returns:
            Dictionary with counts: {reverted: N, skipped: M, errors: K}
        """
        stats = {'reverted': 0, 'skipped': 0, 'errors': 0}
        
        try:
            # Find all audio files recursively
            audio_files: List[Path] = []
            for ext in self.AUDIO_EXTENSIONS:
                audio_files.extend(root_path.rglob(f'*{ext}'))
            
            logger.info(f"Encontrados {len(audio_files)} archivos de audio")
            
            for audio_path in audio_files:
                try:
                    # Check if audio has MUSEE marker in metadata
                    is_musee = self._has_musee_marker_in_audio(audio_path)
                    
                    if is_musee:
                        logger.info(
                            f"Marcador MUSEE detectado en metadata de: {audio_path}"
                        )
                        
                        if not dry_run:
                            try:
                                self._remove_image_from_audio(audio_path)
                                logger.info(f"Imagen removida de metadatos: {audio_path}")
                                stats['reverted'] += 1
                            except Exception as e:
                                logger.error(f"Error removiendo imagen de {audio_path}: {e}")
                                stats['errors'] += 1
                        else:
                            logger.info(f"[DRY RUN] Se removería imagen de: {audio_path}")
                            stats['reverted'] += 1
                    else:
                        logger.debug(f"Sin marcador MUSEE (audio preexistente): {audio_path}")
                        stats['skipped'] += 1
                        
                except Exception as e:
                    logger.error(f"Error procesando {audio_path}: {e}")
                    stats['errors'] += 1
                    
        except Exception as e:
            logger.error(f"Error en revert_song_images: {e}")
            stats['errors'] += 1
            
        return stats
    
    def _has_musee_marker_in_audio(self, audio_path: Path) -> bool:
        """
        Check if audio file has MUSEE marker in metadata.
        
        Args:
            audio_path: Path to audio file
            
        Returns:
            True if MUSEE marker found, False otherwise
            
        Note:
            Corrupted/invalid audio files are skipped silently (return False)
        """
        try:
            audio_file = MutagenFile(audio_path)
            
            if audio_file is None:
                return False
            
            # Check for MUSEE_MARKER in different tag formats
            # MP3 (ID3)
            if hasattr(audio_file, 'tags') and audio_file.tags is not None:
                if 'TXXX:MUSEE_MARKER' in audio_file.tags or \
                   any('MUSEE_MARKER' in str(tag) for tag in audio_file.tags.keys()):
                    return True
            
            # FLAC, OGG (Vorbis comments)
            if 'MUSEE_MARKER' in audio_file:
                return True
            
            # MP4 (iTunes atoms)
            if 'com.apple.itunes.MUSEE_MARKER' in audio_file:
                return True
            
            return False
            
        except Exception as e:
            # Corrupted/invalid audio files are skipped gracefully
            # This is expected for corrupted MP3s, etc.
            logger.debug(f"Skipping invalid audio file (corrupted tags?): {audio_path}: {e}")
            return False
    
    def _remove_image_from_audio(self, audio_path: Path) -> None:
        """
        Remove embedded image from audio file while preserving audio quality.
        
        Args:
            audio_path: Path to audio file
            
        Raises:
            Exception: If file is corrupted or cannot be modified
        """
        try:
            audio_file = MutagenFile(audio_path)
            
            if audio_file is None:
                logger.warning(f"Cannot load audio file (corrupted or unsupported): {audio_path}")
                raise ValueError(f"Cannot read audio file: {audio_path}")
            
            # Remove cover art based on format
            extension = audio_path.suffix.lower()
            
            if extension == '.mp3':
                # ID3 - remove APIC frames
                if hasattr(audio_file, 'tags') and audio_file.tags is not None:
                    audio_file.tags.delall('APIC')
                    audio_file.save()
                    
            elif extension == '.flac':
                # FLAC - clear pictures
                audio_file.clear_pictures()
                audio_file.save()
                
            elif extension in ('.m4a', '.mp4'):
                # MP4 - remove covr atom
                if 'covr' in audio_file:
                    del audio_file['covr']
                    audio_file.save()
                    
            elif extension == '.ogg':
                # OGG Vorbis - remove METADATA_BLOCK_PICTURE
                if 'METADATA_BLOCK_PICTURE' in audio_file:
                    del audio_file['METADATA_BLOCK_PICTURE']
                    audio_file.save()
                    
            elif extension == '.wav':
                # WAV with ID3
                if hasattr(audio_file, 'tags') and audio_file.tags is not None:
                    audio_file.tags.delall('APIC')
                    audio_file.save()
            
            logger.info(f"Image removed from metadata: {audio_path}")
            
        except Exception as e:
            # Log detailed error about corrupted file
            error_msg = str(e)
            if 'sync' in error_msg.lower() or 'frame' in error_msg.lower() or 'mpeg' in error_msg.lower():
                logger.debug(f"Skipping corrupted MP3 (invalid MPEG frame): {audio_path}")
            else:
                logger.debug(f"Cannot modify audio file: {audio_path}: {e}")
            raise
    
    def get_musee_images_stats(self, root_path: Path) -> Dict[str, any]:
        """
        Get statistics about MUSEE-downloaded images without modifying them.
        
        Args:
            root_path: Root directory to search
            
        Returns:
            Dictionary with statistics about found MUSEE images
        """
        stats = {
            'folder_images': {'marked': 0, 'unmarked': 0, 'errors': 0},
            'song_images': {'marked': 0, 'unmarked': 0, 'errors': 0},
            'total_marked': 0,
            'total_unmarked': 0,
            'total_errors': 0,
        }
        
        # Check folder images
        for folder_image in root_path.rglob(self.FOLDER_IMAGE_NAME):
            try:
                is_musee, marker = detect_musee_image(folder_image)
                if is_musee:
                    stats['folder_images']['marked'] += 1
                    stats['total_marked'] += 1
                else:
                    stats['folder_images']['unmarked'] += 1
                    stats['total_unmarked'] += 1
            except Exception as e:
                logger.debug(f"Error checking {folder_image}: {e}")
                stats['folder_images']['errors'] += 1
                stats['total_errors'] += 1
        
        # Check audio files
        for ext in self.AUDIO_EXTENSIONS:
            for audio_file in root_path.rglob(f'*{ext}'):
                try:
                    if self._has_musee_marker_in_audio(audio_file):
                        stats['song_images']['marked'] += 1
                        stats['total_marked'] += 1
                    else:
                        stats['song_images']['unmarked'] += 1
                        stats['total_unmarked'] += 1
                except Exception as e:
                    logger.debug(f"Error checking {audio_file}: {e}")
                    stats['song_images']['errors'] += 1
                    stats['total_errors'] += 1
        
        return stats
