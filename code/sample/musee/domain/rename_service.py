"""File and folder renaming/normalization service."""

import json
import logging
from pathlib import Path
from typing import Dict, List, Tuple
import re

logger = logging.getLogger(__name__)

# Supported audio formats
AUDIO_FORMATS = {'.mp3', '.flac', '.m4a', '.ogg', '.wma', '.wav', '.aac'}

# Version/Remix keywords to extract and put in brackets
# Note: 'mix' is NOT included as a standalone keyword to avoid extracting it from titles like 'Max Mix 1'
# Multi-word phrases and longer keywords are listed first to ensure they're matched before individual words
VERSION_KEYWORDS = [
    # Multi-word phrases (match these first)
    'anniversary edition', 'expanded edition', 'special edition',
    'deluxe edition', 'bonus edition', 'remastered edition',
    'extended version', 'extended mix',
    'radio version', 'radio mix', 'radio edit',
    'original mix', 'club mix', 'dance mix', 'dub mix',
    'instrumental mix', 'acoustic mix',
    # Longer single words (before shorter variants)
    'remix', 'remixed',
    'remastered',  # Must come before 'remaster'
    'remaster',
    'extended',
    'instrumental',
    'acoustic',
    'vocal', 'vocals',
    'unplugged',
    'live',
    'cover',
    'version',
    'edit', 'edition',
    'dub',
    'reissue',
    'deluxe',
]


class RenameService:
    """Normalize and rename music files and folders."""
    
    def __init__(self, dry_run: bool = False, debug: bool = False):
        """
        Initialize rename service.
        
        Args:
            dry_run: If True, don't rename files, just log what would be done
            debug: If True, show verbose logging
        """
        self.dry_run = dry_run
        self.debug = debug
        self.rename_history = {}
        self.undo_state_file = Path.home() / ".musee" / "rename_state.json"
    
    @staticmethod
    def _title_case(text: str) -> str:
        """
        Convert text to title case.
        
        Preserves:
        - All-caps acronyms with periods (e.g., B.O.S.E., U.S.A.)
        - All-caps words that are already uppercase
        
        Args:
            text: Text to convert
            
        Returns:
            Title-cased text
        """
        if not text:
            return text
        
        words = text.split()
        result = []
        
        for word in words:
            # Check if it's an all-caps acronym with periods (e.g., B.O.S.E.)
            if re.match(r'^[A-Z](?:\.[A-Z])+\.?$', word):
                # Keep as-is
                result.append(word)
            # Check if it's already all caps and has no lowercase letters
            elif word.isupper() and len(word) > 1:
                # Keep as-is (it's an acronym like USA, UK, etc.)
                result.append(word)
            else:
                # Apply normal title case
                result.append(word.capitalize())
        
        return ' '.join(result)
    
    @staticmethod
    def _parse_audio_filename(filename: str) -> Dict:
        """
        Parse audio filename to extract metadata.
        
        Attempts to extract: album_order, artist, title, year, extra_details
        
        Args:
            filename: Base filename without extension
            
        Returns:
            Dictionary with parsed metadata
        """
        metadata = {
            'album_order': None,
            'artist': None,
            'title': None,
            'year': None,
            'extra_details': None,
            'original': filename
        }
        
        # First, try to extract a year (1950-2030) from the filename
        year_match = re.search(r'\b(19[5-9]\d|20[0-2]\d)\b', filename)
        year_str = year_match.group(1) if year_match else None
        
        # Remove year and brackets from working text for parsing
        working_text = filename
        if year_str:
            # Remove year and surrounding parentheses if present
            working_text = re.sub(rf'\s*\(\s*{year_str}\s*\)', '', working_text)
            working_text = re.sub(rf'\s*\[\s*{year_str}\s*\]', '', working_text)
            # Also remove bare year
            working_text = working_text.replace(year_str, '')
        
        # Remove trailing/leading dashes and spaces
        working_text = re.sub(r'^[-\s]+|[-\s]+$', '', working_text).strip()
        
        # Extract explicit brackets [...] for extra details
        extra_match = re.search(r'\[(.+?)\]', working_text)
        extra_details = extra_match.group(1).strip() if extra_match else None
        
        # Remove brackets from working text
        working_text = re.sub(r'\[.+?\]', '', working_text).strip()
        
        # Clean up empty parentheses
        working_text = re.sub(r'\s*\(\s*\)\s*', ' ', working_text).strip()
        working_text = re.sub(r'\s*\[\s*\]\s*', ' ', working_text).strip()
        
        # Now extract version keywords from the title/artist parts
        version_keywords_found = []
        
        # Try pattern: order - artist - title [with version keywords possibly in title]
        pattern1 = r'^(\d+)\s*-\s*(.+?)\s*-\s*(.+)$'
        match = re.match(pattern1, working_text)
        if match:
            metadata['album_order'] = match.group(1)
            metadata['artist'] = match.group(2).strip()
            title_part = match.group(3).strip()
            
            # Extract version keywords from title
            artist_clean, title_clean, keywords = RenameService._extract_version_keywords(title_part)
            metadata['title'] = artist_clean if artist_clean else title_clean
            version_keywords_found.extend(keywords)
        else:
            # Try pattern: artist - title [with version keywords possibly in title]
            pattern2 = r'^(.+?)\s*-\s*(.+)$'
            match = re.match(pattern2, working_text)
            if match:
                artist_part = match.group(1).strip()
                title_part = match.group(2).strip()
                
                # Extract version keywords from title
                _, title_clean, keywords = RenameService._extract_version_keywords(title_part)
                metadata['artist'] = artist_part
                metadata['title'] = title_clean
                version_keywords_found.extend(keywords)
            else:
                # Just title - extract keywords from it
                _, title_clean, keywords = RenameService._extract_version_keywords(working_text)
                metadata['title'] = title_clean if title_clean else working_text
                version_keywords_found.extend(keywords)
        
        # Set the year
        if year_str:
            metadata['year'] = year_str
        
        # Set extra details - use extracted keywords if no explicit bracket content
        if not extra_details and version_keywords_found:
            extra_details = ' '.join(version_keywords_found)
        
        # Set extra details (but not if it's just a year)
        if extra_details and not re.match(r'^\d{4}$', extra_details):
            metadata['extra_details'] = extra_details
        
        return metadata
    
    @staticmethod
    def _extract_version_keywords(text: str) -> Tuple[str, str, List[str]]:
        """
        Extract version keywords from text.
        
        Args:
            text: Text to extract keywords from
            
        Returns:
            Tuple of (removed_text, cleaned_text, keywords_found)
        """
        keywords_found = []
        working_text = text
        
        # Extract format indicators: FLAC, CD, LP, 12", Album, Maxi-single (case-insensitive, as standalone words)
        format_patterns = [
            (r'\bFLAC\b', 'FLAC'),
            (r'\bCD\b', 'CD'),
            (r'\bLP\b', 'LP'),
            (r'\b12[\"\']|\b12\b', '12"'),  # Match 12" or 12'  or just 12
            (r'\bAlbum\b', 'Album'),
            (r'\bMaxi-single\b', 'Maxi-single'),
        ]
        
        for pattern, format_name in format_patterns:
            matches = list(re.finditer(pattern, working_text, re.IGNORECASE))
            if matches:
                keywords_found.append(format_name)
                # Remove format from working text (in reverse order to preserve indices)
                for match in reversed(matches):
                    working_text = working_text[:match.start()] + ' ' + working_text[match.end():]
        
        # Extract CDRIP format indicator (case-insensitive, as standalone word or in parentheses)
        cdrip_pattern = r'\b[Cc][Dd][Rr][Ii][Pp]\b'
        cdrip_matches = list(re.finditer(cdrip_pattern, working_text))
        if cdrip_matches:
            keywords_found.append('Cdrip')
            # Remove CDRIP from working text (in reverse order to preserve indices)
            for match in reversed(cdrip_matches):
                working_text = working_text[:match.start()] + ' ' + working_text[match.end():]
        
        # Extract bitrate information (e.g., 192Kbps, 320kbps, 128KBPS)
        # Preserve the case as found in the original text
        bitrate_pattern = r'\b(\d+)\s*([Kk][Bb][Pp][Ss]|[Kk][Bb]/[Ss])\b'
        bitrate_matches = list(re.finditer(bitrate_pattern, working_text))
        if bitrate_matches:
            for match in reversed(bitrate_matches):
                # Preserve the original case from the file
                bitrate_text = match.group(1) + match.group(2)  # e.g., "192Kbps"
                keywords_found.insert(0, bitrate_text)  # Insert at beginning to preserve order
                working_text = working_text[:match.start()] + ' ' + working_text[match.end():]
        
        # Special handling for edition-related patterns like "Expanded & Remastered Edition"
        # These should be captured as complete phrases with everything in between
        # Match: (Expanded/Deluxe/Special/etc) ... (Edition/Remastered/Remaster) [optionally followed by Edition]
        edition_pattern = r'((?:Expanded|Deluxe|Special|Bonus|Anniversary).*?(?:Edition|Remastered|Remaster)(?:\s+Edition)?)'
        for match in re.finditer(edition_pattern, working_text, re.IGNORECASE):
            phrase = match.group(1)
            keywords_found.append(phrase)
            # Mark for removal
            working_text = working_text[:match.start()] + ' ' + working_text[match.end():]
        
        # Build regex for all version keywords (longer phrases first to avoid partial matches)
        # Sort by length descending to match longer phrases first
        sorted_keywords = sorted(VERSION_KEYWORDS, key=len, reverse=True)
        keywords_pattern = '|'.join(re.escape(kw) for kw in sorted_keywords)
        
        # Extract all matching keywords using case-insensitive matching
        for match in re.finditer(rf'({keywords_pattern})', working_text, re.IGNORECASE):
            keyword = match.group(1)
            keywords_found.append(keyword)
        
        # Remove keywords from working text (do this in reverse order of occurrence to preserve indices)
        for match in reversed(list(re.finditer(rf'({keywords_pattern})', working_text, re.IGNORECASE))):
            # Replace with space to avoid merging words
            working_text = working_text[:match.start()] + ' ' + working_text[match.end():]
        
        # Clean up multiple spaces
        working_text = re.sub(r'\s+', ' ', working_text).strip()
        
        return '', working_text, keywords_found
    
    def _normalize_filename(self, metadata: Dict) -> str:
        """
        Build normalized filename from metadata.
        
        Format: $album_order - $Artist - $Title ($year) [$extradetails]
        - album_order: Only if it exists AND equals year
        - year: Always if exists
        - extra_details: Only if it existed before
        
        Args:
            metadata: Parsed metadata dictionary
            
        Returns:
            Normalized filename (without extension)
        """
        parts = []
        
        # Add album order only if it exists and equals year
        if metadata.get('album_order') and metadata.get('year'):
            try:
                if int(metadata['album_order']) == int(metadata['year']):
                    parts.append(metadata['album_order'])
            except (ValueError, TypeError):
                pass
        
        # Add artist
        if metadata.get('artist'):
            parts.append(self._title_case(metadata['artist']))
        
        # Add title
        if metadata.get('title'):
            title_text = self._title_case(metadata['title'])
            # Clean up empty parentheses/brackets
            title_text = re.sub(r'\s*\(\s*\)\s*', ' ', title_text)
            title_text = re.sub(r'\s*\[\s*\]\s*', ' ', title_text)
            title_text = re.sub(r'\s+', ' ', title_text).strip()
            if title_text:  # Only add if not empty after cleanup
                parts.append(title_text)
        
        # Build base with dashes
        if not parts:
            base = metadata.get('original', 'Unknown')
        else:
            base = ' - '.join(parts)
        
        # Add year in parentheses
        if metadata.get('year'):
            base = f"{base} ({metadata['year']})"
        
        # Add extra details in brackets
        if metadata.get('extra_details'):
            extra = metadata['extra_details']
            # Check if this contains multiple separate items (bitrate + other keywords)
            # If so, create separate brackets for each
            parts = extra.split(' ')
            
            # Check if this looks like it has bitrate info
            has_bitrate = any(re.match(r'^\d+[Kk][Bb][Pp][Ss]$', part) for part in parts)
            
            # Check if this has format indicators (CD, LP, FLAC, Cdrip, Album, Maxi-single, 12")
            has_format = any(part.upper() in ['CD', 'LP', 'FLAC', 'CDRIP', 'ALBUM', 'MAXI-SINGLE', '12"'] or 
                           part.upper() == '12' for part in parts)
            
            if (has_bitrate or has_format) and len(parts) > 1:
                # Multiple brackets for multiple items
                for part in parts:
                    if part:
                        # Preserve case for bitrate, lowercase others
                        if re.match(r'^\d+[Kk][Bb][Pp][Ss]$', part):
                            base = f"{base} [{part}]"
                        else:
                            base = f"{base} [{part.lower()}]"
            else:
                # Single bracket for the whole thing (original behavior)
                base = f"{base} [{extra.lower()}]"
        
        return base
    
    def normalize_files(self, root_path: Path) -> int:
        """
        Normalize all music files and folders recursively.
        
        Args:
            root_path: Root directory to scan
            
        Returns:
            Number of items renamed
        """
        if not root_path.exists():
            logger.error(f"Path does not exist: {root_path}")
            return 0
        
        if not root_path.is_dir():
            logger.error(f"Path is not a directory: {root_path}")
            return 0
        
        logger.info(f"📝 Starting file/folder normalization for: {root_path}")
        
        renamed_count = 0
        renamed_count += self._normalize_recursive(root_path)
        
        # Update playlists after renaming
        self._update_playlists_after_rename(root_path)
        
        # Save rename history for undo
        if not self.dry_run and self.rename_history:
            self._save_rename_history(root_path)
        
        logger.info(f"✅ Normalization complete!")
        logger.info(f"   📝 Items renamed: {renamed_count}")
        
        return renamed_count
    
    def _update_playlists_after_rename(self, root_path: Path) -> None:
        """
        Update playlist file paths after renaming files/folders.
        
        Args:
            root_path: Root directory to scan for playlists
        """
        if self.dry_run:
            return
        
        try:
            # Find all .m3u files recursively
            for playlist_path in root_path.rglob('*.m3u'):
                self._update_playlist_content(playlist_path)
        except Exception as e:
            logger.warning(f"⚠️  Error updating playlists: {e}")
    
    def _update_playlist_content(self, playlist_path: Path) -> None:
        """
        Update the content of a single playlist to reflect renamed files.
        
        Args:
            playlist_path: Path to the .m3u file
        """
        try:
            content = playlist_path.read_text(encoding='utf-8')
            updated_content = content
            
            # For each renamed item, update references in the playlist
            for new_path_str, old_path_str in self.rename_history.items():
                old_path = Path(old_path_str)
                new_path = Path(new_path_str)
                
                # Replace old path references with new ones
                # Handle both forward and backward slashes
                old_name = old_path.name
                new_name = new_path.name
                
                updated_content = updated_content.replace(old_name, new_name)
            
            # Only write if content changed
            if updated_content != content:
                playlist_path.write_text(updated_content, encoding='utf-8')
                logger.info(f"✅ Updated playlist: {playlist_path.name}")
        except Exception as e:
            logger.warning(f"⚠️  Error updating playlist {playlist_path}: {e}")
    
    def _normalize_recursive(self, folder: Path) -> int:
        """
        Recursively normalize files and folders.
        
        Args:
            folder: Directory to process
            
        Returns:
            Number of items renamed
        """
        renamed_count = 0
        
        # First normalize files in this folder
        try:
            for item in sorted(folder.iterdir()):
                if item.is_file() and item.suffix.lower() in AUDIO_FORMATS:
                    # Parse and normalize filename
                    stem = item.stem
                    metadata = self._parse_audio_filename(stem)
                    normalized_stem = self._normalize_filename(metadata)
                    
                    if normalized_stem != stem:
                        new_path = item.parent / f"{normalized_stem}{item.suffix}"
                        renamed_count += self._rename_file(item, new_path)
        except PermissionError:
            logger.warning(f"⚠️  Permission denied: {folder}")
        except Exception as e:
            logger.warning(f"⚠️  Error processing files in {folder}: {e}")
        
        # Then normalize folders in this directory
        try:
            for item in sorted(folder.iterdir()):
                if item.is_dir() and not item.name.startswith('.'):
                    # Parse and normalize folder name (same as files)
                    folder_name = item.name
                    metadata = self._parse_audio_filename(folder_name)
                    normalized_name = self._normalize_filename(metadata)
                    
                    if normalized_name != folder_name:
                        new_path = item.parent / normalized_name
                        renamed_count += self._rename_folder(item, new_path)
                        # Process the renamed folder
                        renamed_count += self._normalize_recursive(new_path)
                    else:
                        # Process the folder as-is
                        renamed_count += self._normalize_recursive(item)
        except PermissionError:
            logger.warning(f"⚠️  Permission denied: {folder}")
        except Exception as e:
            logger.warning(f"⚠️  Error processing folders in {folder}: {e}")
        
        return renamed_count
    
    def _rename_file(self, old_path: Path, new_path: Path) -> int:
        """
        Rename a single file.
        
        Args:
            old_path: Current file path
            new_path: New file path
            
        Returns:
            1 if renamed, 0 otherwise
        """
        if self.dry_run:
            logger.info(f"[DRY RUN] Would rename: {old_path.name}")
            logger.info(f"[DRY RUN]           to: {new_path.name}")
            return 1
        
        try:
            old_path.rename(new_path)
            logger.info(f"✅ Renamed file: {old_path.name} → {new_path.name}")
            
            # Track for undo
            self.rename_history[str(new_path)] = str(old_path)
            return 1
        except Exception as e:
            logger.error(f"❌ Error renaming {old_path}: {e}")
            return 0
    
    def _rename_folder(self, old_path: Path, new_path: Path) -> int:
        """
        Rename a single folder.
        
        Args:
            old_path: Current folder path
            new_path: New folder path
            
        Returns:
            1 if renamed, 0 otherwise
        """
        if self.dry_run:
            logger.info(f"[DRY RUN] Would rename folder: {old_path.name}")
            logger.info(f"[DRY RUN]                   to: {new_path.name}")
            return 1
        
        try:
            old_path.rename(new_path)
            logger.info(f"✅ Renamed folder: {old_path.name} → {new_path.name}")
            
            # Track for undo
            self.rename_history[str(new_path)] = str(old_path)
            return 1
        except Exception as e:
            logger.error(f"❌ Error renaming folder {old_path}: {e}")
            return 0
    
    def _save_rename_history(self, root_path: Path) -> None:
        """Save rename history for undo capability."""
        try:
            self.undo_state_file.parent.mkdir(parents=True, exist_ok=True)
            
            state_data = {
                'root_path': str(root_path),
                'renames': self.rename_history
            }
            
            with open(self.undo_state_file, 'w') as f:
                json.dump(state_data, f, indent=2)
            
            logger.debug(f"Saved rename history to {self.undo_state_file}")
        except Exception as e:
            logger.warning(f"⚠️  Could not save rename history: {e}")
    
    def revert_renames(self, root_path: Path) -> int:
        """
        Revert all renames from the last normalization.
        
        Args:
            root_path: Root directory (used to verify)
            
        Returns:
            Number of items reverted
        """
        if not self.undo_state_file.exists():
            logger.info("No rename history found")
            return 0
        
        logger.info(f"🔄 Reverting renames from: {root_path}")
        
        reverted_count = 0
        
        try:
            with open(self.undo_state_file, 'r') as f:
                state_data = json.load(f)
            
            renames = state_data.get('renames', {})
            
            # Reverse the renames (go backwards to handle nested folders)
            for new_path_str, old_path_str in reversed(list(renames.items())):
                new_path = Path(new_path_str)
                old_path = Path(old_path_str)
                
                if new_path.exists():
                    if self.dry_run:
                        logger.info(f"[DRY RUN] Would revert: {new_path.name} → {old_path.name}")
                        reverted_count += 1
                    else:
                        try:
                            new_path.rename(old_path)
                            logger.info(f"✅ Reverted: {new_path.name} → {old_path.name}")
                            reverted_count += 1
                        except Exception as e:
                            logger.warning(f"⚠️  Could not revert {new_path.name}: {e}")
            
            # Delete the history file after successful revert
            if not self.dry_run and reverted_count > 0:
                self.undo_state_file.unlink(missing_ok=True)
            
            logger.info(f"✅ Revert complete! {reverted_count} items reverted")
        except Exception as e:
            logger.error(f"❌ Error reverting renames: {e}")
        
        return reverted_count
