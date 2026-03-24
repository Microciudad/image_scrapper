"""Playlist generation service for recursive music directory structures."""

import logging
from pathlib import Path
from typing import Set

logger = logging.getLogger(__name__)

# Supported audio formats
AUDIO_FORMATS = {'.mp3', '.flac', '.m4a', '.ogg', '.wma', '.wav', '.aac'}


class PlaylistGenerator:
    """Generate M3U playlists for music folder hierarchies."""
    
    def __init__(self, dry_run: bool = False, debug: bool = False, skip_single_file: bool = False):
        """
        Initialize playlist generator.
        
        Args:
            dry_run: If True, don't create files, just log what would be done
            debug: If True, show verbose logging
            skip_single_file: If True, skip creating playlists for folders with only 1 song
        """
        self.dry_run = dry_run
        self.debug = debug
        self.skip_single_file = skip_single_file
        self.playlists_created = 0
        self.songs_added = 0
        self.skipped_count = 0
    
    def generate_playlists(self, root_path: Path) -> int:
        """
        Generate M3U playlists recursively for all folders.
        
        Args:
            root_path: Root directory to scan
            
        Returns:
            Number of playlists created
        """
        if not root_path.exists():
            logger.error(f"Path does not exist: {root_path}")
            return 0
        
        if not root_path.is_dir():
            logger.error(f"Path is not a directory: {root_path}")
            return 0
        
        logger.info(f"🎵 Starting playlist generation for: {root_path}")
        
        # Generate playlists for all directories
        self._generate_playlists_recursive(root_path)
        
        logger.info(f"✅ Playlist generation complete!")
        logger.info(f"   📋 Playlists created: {self.playlists_created}")
        logger.info(f"   🎵 Total songs added: {self.songs_added}")
        if self.skipped_count > 0:
            logger.info(f"   ⏭️  Skipped (single file): {self.skipped_count}")
        
        return self.playlists_created
    
    def _generate_playlists_recursive(self, folder: Path) -> None:
        """
        Recursively generate playlists for a folder and all subfolders.
        
        Args:
            folder: Directory to process
        """
        # Get all songs in this folder and subfolders
        all_songs = self._get_all_songs_recursive(folder)
        
        if all_songs:
            # Skip if single file and skip_single_file is enabled
            if self.skip_single_file and len(all_songs) == 1:
                logger.info(f"⏭️  Skipped (1 file): {folder.name}")
                self.skipped_count += 1
            else:
                # Create playlist for this folder
                playlist_name = folder.name or "Music"
                playlist_path = folder / f"{playlist_name}.m3u"
                
                self._create_playlist(playlist_path, all_songs, folder)
                self.playlists_created += 1
                self.songs_added += len(all_songs)
        
        # Process all subdirectories
        try:
            for item in sorted(folder.iterdir()):
                if item.is_dir() and not item.name.startswith('.'):
                    self._generate_playlists_recursive(item)
        except PermissionError:
            logger.warning(f"⚠️  Permission denied: {folder}")
        except Exception as e:
            logger.warning(f"⚠️  Error processing folder {folder}: {e}")
    
    def _get_all_songs_recursive(self, folder: Path) -> list:
        """
        Get all songs in folder and subfolders.
        
        Args:
            folder: Directory to scan
            
        Returns:
            List of relative paths to audio files
        """
        songs = []
        
        try:
            for item in sorted(folder.rglob('*')):
                if item.is_file() and item.suffix.lower() in AUDIO_FORMATS:
                    # Get relative path from the playlist location
                    rel_path = item.relative_to(folder)
                    songs.append(rel_path)
        except PermissionError:
            logger.warning(f"⚠️  Permission denied: {folder}")
        except Exception as e:
            logger.warning(f"⚠️  Error scanning {folder}: {e}")
        
        return songs
    
    def _create_playlist(self, playlist_path: Path, songs: list, base_folder: Path) -> None:
        """
        Create M3U playlist file (overwrites if exists).
        
        Args:
            playlist_path: Path to save playlist (will overwrite existing)
            songs: List of relative paths to songs
            base_folder: Base folder for relative paths
        """
        if not songs:
            return
        
        # Build M3U content
        m3u_content = "#EXTM3U\n"
        
        for song in songs:
            # Create entry in M3U format
            m3u_content += f"#EXTINF:-1,{song.stem}\n"
            m3u_content += f"{song}\n"
        
        if self.dry_run:
            action = "overwrite" if playlist_path.exists() else "create"
            logger.info(f"[DRY RUN] Would {action}: {playlist_path}")
            logger.info(f"[DRY RUN]   with {len(songs)} songs")
            return
        
        try:
            # write_text() automatically overwrites if file exists
            playlist_path.write_text(m3u_content, encoding='utf-8')
            action = "Updated" if playlist_path.exists() else "Created"
            logger.info(f"✅ {action} playlist: {playlist_path.name} ({len(songs)} songs)")
        except Exception as e:
            logger.error(f"❌ Error creating playlist {playlist_path}: {e}")
    
    def remove_playlists(self, root_path: Path) -> int:
        """
        Remove all M3U playlists created by MUSEE (recursively).
        
        Identifies playlists by matching folder name with .m3u filename
        (e.g., folder 'Rock' contains 'Rock.m3u').
        
        Args:
            root_path: Root directory to scan
            
        Returns:
            Number of playlists removed
        """
        if not root_path.exists():
            logger.error(f"Path does not exist: {root_path}")
            return 0
        
        if not root_path.is_dir():
            logger.error(f"Path is not a directory: {root_path}")
            return 0
        
        logger.info(f"🎵 Removing MUSEE playlists from: {root_path}")
        
        playlists_removed = 0
        playlists_removed += self._remove_playlists_recursive(root_path)
        
        logger.info(f"✅ Playlist removal complete!")
        logger.info(f"   🗑️  Playlists removed: {playlists_removed}")
        
        return playlists_removed
    
    def _remove_playlists_recursive(self, folder: Path) -> int:
        """
        Recursively remove playlists from a folder and all subfolders.
        
        Args:
            folder: Directory to process
            
        Returns:
            Number of playlists removed
        """
        removed_count = 0
        
        # Check if a playlist exists for this folder
        playlist_path = folder / f"{folder.name}.m3u"
        
        if playlist_path.exists():
            if self.dry_run:
                logger.info(f"[DRY RUN] Would remove: {playlist_path}")
            else:
                try:
                    playlist_path.unlink()
                    logger.info(f"🗑️  Removed: {playlist_path.name}")
                    removed_count += 1
                except Exception as e:
                    logger.warning(f"⚠️  Error removing {playlist_path}: {e}")
        
        # Process all subdirectories
        try:
            for item in sorted(folder.iterdir()):
                if item.is_dir() and not item.name.startswith('.'):
                    removed_count += self._remove_playlists_recursive(item)
        except PermissionError:
            logger.warning(f"⚠️  Permission denied: {folder}")
        except Exception as e:
            logger.warning(f"⚠️  Error processing folder {folder}: {e}")
        
        return removed_count
