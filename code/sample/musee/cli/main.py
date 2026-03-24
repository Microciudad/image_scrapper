"""Main CLI interface for musee."""

import click
import re
from pathlib import Path
from typing import Optional
from dataclasses import dataclass

from .. import configure_logging
from ..domain.services import MusicImageService
from ..domain.action_executor import ActionExecutor, ActionType

# Load version
try:
    version_file = Path(__file__).parent.parent / "VERSION"
    with open(version_file, "r") as f:
        __version__ = f.read().strip()
except Exception:
    __version__ = "1.0.0"
from ..domain.image_source_adapters import DiscogsImageSourceAdapter, GoogleImageSourceAdapter
from ..domain.undo_service import UndoService
from ..infrastructure.repositories import ImageRepository
from ..infrastructure.simple_discogs import SimpleDiscogsRepository
from ..infrastructure.google_image_scraper import GoogleImageScraper


def parse_size_string(size_str: str) -> int:
    """
    Convierte una cadena de tamaño a bytes.
    
    Soporta: 100, 100b, 100B, 100kb, 100KB, 100K, 100mb, 100MB, 100M, 100gb, 100GB, 100G
    
    Args:
        size_str: Cadena de tamaño (ej: "25k", "1.5mb", "100")
        
    Returns:
        Tamaño en bytes
        
    Raises:
        ValueError: Si el formato no es válido
    """
    size_str = size_str.strip().lower()
    
    # Intentar match con patrón: número + unidad opcional
    match = re.match(r'^([\d.]+)\s*([kmg]?b?)?$', size_str)
    if not match:
        raise ValueError(f"Formato de tamaño inválido: {size_str}. Use: 100, 100k, 1.5m, etc.")
    
    number_str, unit = match.groups()
    
    try:
        number = float(number_str)
    except ValueError:
        raise ValueError(f"Número inválido: {number_str}")
    
    # Normalizar unidad
    unit = (unit or '').rstrip('b').lower()
    
    multipliers = {
        '': 1,
        'k': 1024,
        'm': 1024 * 1024,
        'g': 1024 * 1024 * 1024,
    }
    
    if unit not in multipliers:
        raise ValueError(f"Unidad desconocida: {unit}. Use: b, k, m, g")
    
    return int(number * multipliers[unit])


class CustomHelpFormatter(click.HelpFormatter):
    """Custom formatter that preserves line breaks and doesn't wrap text."""
    def __init__(self, *args, **kwargs):
        # Set width to a very large value to prevent wrapping
        kwargs['width'] = 9999
        super().__init__(*args, **kwargs)
    
    def write_text(self, text):
        """Write text preserving formatting."""
        self.write(text)


class CustomCommand(click.Command):
    """Custom command with custom formatter."""
    def get_help(self, ctx):
        """Get help text."""
        formatter = CustomHelpFormatter()
        self.format_help(ctx, formatter)
        return formatter.getvalue()
    
    def format_help_text(self, ctx, formatter):
        """Write the help text to the formatter."""
        text = self.help or ""
        if text:
            formatter.write("\n" + text + "\n")


class CustomGroup(click.Group):
    """Custom group that adds credits to help."""
    def format_help(self, ctx, formatter):
        """Add credits at the end of help."""
        super().format_help(ctx, formatter)
        formatter.write("\n")
        formatter.write("=" * 80 + "\n")
        formatter.write("Author: Oscar Alvarez\n")
        formatter.write("Project: https://github.com/microciudad/musee/\n")
        formatter.write("=" * 80 + "\n")


@dataclass
class ProcessingInfo:
    """Información sobre el procesamiento actual."""
    file_path: Path
    current: int
    total: int
    item_type: str  # 'folder', 'song', 'album_song'
    artist: Optional[str] = None
    title: Optional[str] = None
    album: Optional[str] = None
    album_type: Optional[str] = None  # 'proper_album', 'loose_collection', 'single'
    version: Optional[str] = None  # versión extraída del título (ej: "Vocal Remix")
    status: Optional[str] = None  # 'skipped', None for normal processing
    
    def format_compact(self) -> str:
        """Formato compacto para una línea de salida."""
        percentage = (self.current / self.total * 100) if self.total > 0 else 0
        
        # Tipo de item
        type_str = self.item_type.upper()
        if self.item_type == 'song':
            if self.album_type == 'proper_album':
                type_str = 'SONG(ALBUM)'
            elif self.album_type == 'loose_collection':
                type_str = 'SONG(VARIOS)'
            elif self.album_type == 'single':
                type_str = 'SONG(SINGLE)'
            else:
                type_str = 'SONG'
        elif self.item_type == 'folder':
            type_str = 'FOLDER(ALBUM)'
        
        # Add status indicator if skipped
        if self.status == 'skipped':
            type_str += '[SKIPPED]'
        
        # Nombre del archivo/carpeta (truncado si es muy largo)
        name = self.file_path.name
        if len(name) > 50:
            name = name[:47] + "..."
        
        # Información de artista/título/álbum con etiquetas claras
        metadata = ""
        if self.item_type == 'folder':
            # Para carpetas, mostrar: Artist: X | Album: Y
            parts = []
            if self.artist:
                parts.append(f"Artist: {self.artist}")
            if self.album:
                parts.append(f"Album: {self.album}")
            if parts:
                metadata = " | " + " | ".join(parts)
        else:
            # Para canciones, mostrar: Artist: X | Song: Y | Version: Z
            if self.artist or self.title or self.version:
                parts = []
                if self.artist:
                    parts.append(f"Artist: {self.artist}")
                if self.title:
                    parts.append(f"Song: {self.title}")
                if self.version:
                    parts.append(f"Version: {self.version}")
                metadata = " | " + " | ".join(parts)
            
            # Información de álbum si existe para canciones
            album_info = ""
            if self.album and self.item_type == 'song':
                album_info = f" [Album: {self.album}]"
            metadata = metadata + (album_info if album_info else "")
        
        # Pad type_str to align all | symbols vertically (max width: FOLDER(ALBUM)[SKIPPED])
        type_str_padded = type_str.ljust(27)
        return f">> [{self.current:3d}/{self.total:3d}] {percentage:5.1f}% | {type_str_padded} | {name}{metadata}"


class LegacyGroup(click.Group):
    """Custom group that supports both legacy format (path action ...) and new format (subcommand ...)"""
    
    def format_help(self, ctx, formatter):
        """Format help using CustomHelpFormatter to prevent text wrapping."""
        # Create a CustomHelpFormatter (no wrapping)
        custom_fmt = CustomHelpFormatter()
        # Call parent with our custom formatter
        super().format_help(ctx, custom_fmt)
        # Write the result to the actual formatter
        formatter.write(custom_fmt.getvalue())
    
    def format_epilog(self, ctx, formatter):
        """Add version and credits at the end."""
        formatter.write(f"\nVersion: v{__version__}\n")
        formatter.write("=" * 80 + "\n")
        formatter.write("Author: Oscar Alvarez\n")
        formatter.write("Project: https://github.com/microciudad/musee/\n")
        formatter.write("=" * 80 + "\n")
    
    def format_commands(self, ctx, formatter):
        """Custom format_commands to show commands in desired order."""
        commands = []
        for subcommand in self.list_commands(ctx):
            cmd = self.get_command(ctx, subcommand)
            if cmd is None or cmd.hidden:
                continue
            
            # Define desired order
            order_map = {
                'process': 0,
                'scrap': 1,
                'scrap_folders': 2,
                'scrap_songs': 3,
                'generate_playlists': 4,
                'normalize_files': 5,
                'revert_renames': 6,
                'revert_images': 7,
                'cleanup_images': 8,
            }
            
            order = order_map.get(subcommand, 999)
            commands.append((order, subcommand, cmd))
        
        # Sort by order
        commands.sort(key=lambda x: (x[0], x[1]))
        
        if commands:
            with formatter.section('Commands'):
                # Calculate max command name length for alignment
                max_len = max(len(subcommand) for _, subcommand, _ in commands)
                
                rows = []
                for _, subcommand, cmd in commands:
                    help_text = cmd.get_short_help_str(100)
                    # Pad command name to align help text
                    padded_name = subcommand.ljust(max_len)
                    rows.append((padded_name, help_text))
                
                formatter.write_dl(rows)
    
    def main(self, *args, **kwargs):
        """Override main to preprocess arguments."""
        import sys
        
        # Get the arguments that would be parsed
        args_to_check = sys.argv[1:] if not args else args[0]
        
        # Look for legacy format: path + action
        if args_to_check and len(args_to_check) >= 2:
            potential_path = args_to_check[0]
            potential_action = args_to_check[1] if len(args_to_check) > 1 else None
            
            # Removed "both" from valid actions - use process command instead
            valid_actions = ['get_folder_image', 'get_song_image', 'undo_images']
            
            # Check: first arg has path separators or 'process' is not being called
            is_path_like = ('/' in potential_path or '\\' in potential_path or 
                           potential_path != 'process')
            
            if is_path_like and potential_action in valid_actions:
                # Insert 'process' as first arg
                if not args:
                    sys.argv.insert(1, 'process')
                else:
                    args_to_check.insert(0, 'process')
                    args = (args_to_check,) + args[1:]
        
        return super().main(*args, **kwargs)
    
    def invoke(self, ctx):
        """Override invoke to show help on invalid commands."""
        try:
            return super().invoke(ctx)
        except click.MissingParameter as e:
            # Handle missing required arguments/options
            param_name = e.param.name if e.param else "argument"
            error_msg = f"Missing required parameter: {param_name}"
            if "path" in param_name.lower():
                error_msg = f"Error: Missing required path argument.\n\nUsage: musee {ctx.invoked_subcommand} <path> [OPTIONS]"
            
            # Show help and then the error
            click.echo(ctx.get_help())
            click.echo()
            click.echo(error_msg, err=True)
            ctx.exit(2)
        except click.UsageError as e:
            # Fix common typos: --- should be --
            error_msg = e.message
            if '---' in error_msg:
                error_msg = error_msg.replace('---', '--')
                error_msg += " (did you mean '--' instead of '---'?)"
            
            # Show help and then the error
            click.echo(ctx.get_help())
            click.echo()
            click.echo(f"Error: {error_msg}", err=True)
            ctx.exit(2)


@click.group(cls=LegacyGroup, invoke_without_command=True)
@click.pass_context
def cli(ctx):
    """MUSEE - Automated Music Art Downloader.

Automatically download and manage album artwork for your music library.
Works with folder covers, song metadata, or both.

Examples - Multiple Actions (NEW):
  musee process /path/to/music --normalize --scrap-folders --scrap-songs --generate-playlists
  musee process /path/to/music --scrap-folders --scrap-songs --force
  musee process /path/to/music --normalize --generate-playlists --dry-run

Examples - Individual Actions (Legacy):
  musee scrap /path/to/music
  musee scrap /path/to/music --force
  musee scrap_folders /g/music/albums
  musee scrap_songs /g/music/albums --force --minimum-size 25k
  musee revert_images /g/music/albums --dry-run

Use 'musee COMMAND --help' for detailed options and more examples.
    """
    if ctx.invoked_subcommand is None:
        click.echo(ctx.get_help())


@cli.command('process', cls=CustomCommand)
@click.argument('path', type=click.Path(exists=True, path_type=Path))
@click.option('--normalize', is_flag=True, help='Rename files/folders with title case')
@click.option('--scrap-folders', is_flag=True, help='Download folder.jpg')
@click.option('--scrap-songs', is_flag=True, help='Embed images in songs')
@click.option('--generate-playlists', is_flag=True, help='Generate M3U playlists')
@click.option('--revert-renames', is_flag=True, help='Undo file and folder renames')
@click.option('--revert-images', is_flag=True, help='Remove MUSEE-created images and playlists')
@click.option('--cleanup-images', is_flag=True, help='Delete ALL images and playlists (destructive)')
@click.option('--force', is_flag=True, help='Force overwrite existing images/metadata')
@click.option('--dry-run', is_flag=True, help='Preview changes without making modifications')
@click.option('--debug', is_flag=True, help='Enable verbose debug logging')
@click.option('--discogs-token', envvar='DISCOGS_TOKEN', help='Discogs API token')
@click.option('--image-source', type=click.Choice(['discogs', 'google', 'google-discogs']), default='google-discogs',
              help='Image source for scraping')
@click.option('--rate-limit', type=float, default=1.0, help='Delay between requests in seconds')
@click.option('--no-rate-limit', is_flag=True, help='Disable rate limiting')
@click.option('--google-min-delay', type=float, default=3.0, help='Min delay for Google requests (default: 3 sec)')
@click.option('--google-max-delay', type=float, default=8.0, help='Max delay for Google requests (default: 8 sec)')
@click.option('--minimum-size', type=str, default=None, help='Only replace if new image is >= this size (e.g., 25k, 1.5m)')
@click.option('--skip-single-file', is_flag=True, help='Skip creating playlists for folders with only 1 song')
@click.option('--folder-matching-only', type=str, default=None, help='Filter: only process folders matching this text')
def process_cmd(path, normalize, scrap_folders, scrap_songs, generate_playlists, revert_renames, revert_images, 
                cleanup_images, force, dry_run, debug, discogs_token, image_source, rate_limit, no_rate_limit, 
                google_min_delay, google_max_delay, minimum_size, skip_single_file, folder_matching_only):
    """Execute multiple actions in optimal sequence.
    
    Select any combination of actions to execute in proper order:
    1. Normalize files (if enabled)
    2. Scrap folder images (if enabled)
    3. Scrap song images (if enabled)
    4. Generate playlists (if enabled)
    
    Exclusive actions (cannot combine with above):
    - Revert renames: Undo file and folder renames
    - Revert images: Remove MUSEE-created images and playlists
    - Cleanup images: Delete ALL images and playlists (destructive)
    """
    configure_logging(debug=debug)
    
    # Validate that at least one action is selected
    if not any([normalize, scrap_folders, scrap_songs, generate_playlists, revert_renames, revert_images, cleanup_images]):
        click.echo(click.style("ERROR: At least one action must be selected", fg="red", bold=True), err=True)
        click.echo("Use --help to see available options")
        return
    
    # Validate that exclusive actions are not combined with other actions
    exclusive_actions = [revert_renames, revert_images, cleanup_images]
    normal_actions = [normalize, scrap_folders, scrap_songs, generate_playlists]
    
    if any(exclusive_actions) and any(normal_actions):
        click.echo(click.style("ERROR: Cannot combine revert/cleanup actions with other actions", fg="red", bold=True), err=True)
        click.echo("Use --help to see available options")
        return
    
    # Validate minimum_size usage
    if minimum_size and not (scrap_folders or scrap_songs):
        click.echo(click.style("WARNING: --minimum-size requires --scrap-folders or --scrap-songs (ignoring)", fg="yellow"))
        minimum_size = None
    
    if minimum_size:
        try:
            parse_size_string(minimum_size)
        except ValueError as e:
            click.echo(click.style(f"ERROR: {e}", fg="red", bold=True))
            return
    
    # Show mode
    if dry_run:
        click.echo(click.style("DRY RUN MODE - Preview only, no changes will be made", fg="cyan", bold=True))
    
    # Build action executor
    executor = ActionExecutor()
    executor.skip_single_file = skip_single_file
    
    if normalize:
        executor.add_action(ActionType.NORMALIZE_FILES)
    if scrap_folders:
        executor.add_action(ActionType.SCRAP_FOLDERS)
    if scrap_songs:
        executor.add_action(ActionType.SCRAP_SONGS)
    if generate_playlists:
        executor.add_action(ActionType.GENERATE_PLAYLISTS)
    if revert_renames:
        executor.add_action(ActionType.REVERT_RENAMES)
    if revert_images:
        executor.add_action(ActionType.REVERT_IMAGES)
    if cleanup_images:
        executor.add_action(ActionType.CLEANUP_IMAGES)
    
    # Prepare options dict to pass to each action
    options = {
        'force': force,
        'dry_run': dry_run,
        'debug': debug,
        'discogs_token': discogs_token,
        'image_source': image_source,
        'rate_limit': rate_limit,
        'no_rate_limit': no_rate_limit,
        'google_min_delay': google_min_delay,
        'google_max_delay': google_max_delay,
        'minimum_size': minimum_size,
        'skip_single_file': skip_single_file,
        'folder_matching_only': folder_matching_only,
    }
    
    # Register callbacks for each action
    executor.set_callback(ActionType.NORMALIZE_FILES, _execute_normalize_files)
    executor.set_callback(ActionType.SCRAP_FOLDERS, _execute_scrap_folders)
    executor.set_callback(ActionType.SCRAP_SONGS, _execute_scrap_songs)
    executor.set_callback(ActionType.GENERATE_PLAYLISTS, _execute_generate_playlists)
    executor.set_callback(ActionType.REVERT_RENAMES, _execute_revert_renames)
    executor.set_callback(ActionType.REVERT_IMAGES, _execute_revert_images)
    executor.set_callback(ActionType.CLEANUP_IMAGES, _execute_cleanup_images)
    
    try:
        results = executor.execute(path, options)
        
        # Show final summary
        click.echo()
        click.echo("=" * 80)
        click.echo(click.style("PROCESS COMPLETE", fg="green", bold=True))
        click.echo("=" * 80)
        
    except Exception as e:
        click.echo(click.style(f"ERROR: {str(e)}", fg="red", bold=True), err=True)
        return


# Callback functions for process command actions

def _execute_normalize_files(path: Path, action: ActionType, options: dict) -> dict:
    """Execute normalize_files action."""
    from ..domain.rename_service import RenameService
    
    service = RenameService(dry_run=options['dry_run'], debug=options['debug'])
    count = service.normalize_files(path)
    
    click.echo()
    if count > 0:
        click.echo(click.style(f"✅ Successfully normalized {count} items!", fg="green", bold=True))
    else:
        click.echo(click.style("ℹ️  No items to normalize", fg="yellow"))
    
    return {'items_processed': count}


def _execute_scrap_folders(path: Path, action: ActionType, options: dict) -> dict:
    """Execute scrap_folders action."""
    # Note: _handle_download doesn't return stats directly in current implementation
    # It just prints them. We'll return an empty dict for now.
    _handle_download(
        path,
        'get_folder_image',
        options['force'],
        options['discogs_token'],
        options['image_source'],
        options['rate_limit'],
        options['no_rate_limit'],
        options['google_min_delay'],
        options['google_max_delay'],
        options['folder_matching_only'],
        options['minimum_size']
    )
    return {'action': 'scrap_folders', 'status': 'completed'}


def _execute_scrap_songs(path: Path, action: ActionType, options: dict) -> dict:
    """Execute scrap_songs action."""
    # Note: _handle_download doesn't return stats directly in current implementation
    # It just prints them. We'll return an empty dict for now.
    _handle_download(
        path,
        'get_song_image',
        options['force'],
        options['discogs_token'],
        options['image_source'],
        options['rate_limit'],
        options['no_rate_limit'],
        options['google_min_delay'],
        options['google_max_delay'],
        options['folder_matching_only'],
        options['minimum_size']
    )
    return {'action': 'scrap_songs', 'status': 'completed'}


def _execute_generate_playlists(path: Path, action: ActionType, options: dict) -> dict:
    """Execute generate_playlists action."""
    from ..domain.playlist_generator import PlaylistGenerator
    
    generator = PlaylistGenerator(
        dry_run=options['dry_run'],
        debug=options['debug'],
        skip_single_file=options['skip_single_file']
    )
    count = generator.generate_playlists(path)
    
    click.echo()
    if count > 0:
        click.echo(click.style(f"✅ Successfully generated {count} playlists!", fg="green", bold=True))
    else:
        click.echo(click.style("ℹ️  No playlists created (no folders with songs found)", fg="yellow"))
    
    return {'playlists_created': count}


def _execute_revert_renames(path: Path, action: ActionType, options: dict) -> dict:
    """Execute revert_renames action."""
    from ..domain.rename_service import RenameService
    
    service = RenameService(dry_run=options['dry_run'], debug=options['debug'])
    count = service.revert_renames(path)
    
    click.echo()
    if count > 0:
        click.echo(click.style(f"Successfully reverted {count} items!", fg="green", bold=True))
    else:
        click.echo(click.style("No renames to revert", fg="yellow"))
    
    return {'items_reverted': count}


def _execute_revert_images(path: Path, action: ActionType, options: dict) -> dict:
    """Execute revert_images action."""
    from ..domain.undo_service import UndoService
    
    undo_service = UndoService()
    count = undo_service.revert_images(path, dry_run=options['dry_run'])
    
    click.echo()
    if count > 0:
        click.echo(click.style(f"Successfully removed {count} image files!", fg="green", bold=True))
    else:
        click.echo(click.style("No images to remove", fg="yellow"))
    
    return {'images_removed': count}


def _execute_cleanup_images(path: Path, action: ActionType, options: dict) -> dict:
    """Execute cleanup_images action."""
    from ..domain.undo_service import UndoService
    
    undo_service = UndoService()
    count = undo_service.cleanup_all_images(path, dry_run=options['dry_run'])
    
    click.echo()
    if count > 0:
        click.echo(click.style(f"Successfully cleaned up {count} files!", fg="green", bold=True))
    else:
        click.echo(click.style("No files to clean up", fg="yellow"))
    
    return {'files_cleaned': count}


@cli.command('scrap_folders', cls=CustomCommand)
@click.argument('path', type=click.Path(exists=True, path_type=Path))
@click.option('--force', is_flag=True, help='Force overwrite existing images')
@click.option('--minimum-size', type=str, default=None, help='Only replace if new image is >= this size (e.g., 25k, 1.5m). Requires --force.')
@click.option('--dry-run', is_flag=True, help='Test mode: preview what will be processed without making changes')
@click.option('--debug', is_flag=True, help='Enable verbose debug logging')
@click.option('--discogs-token', envvar='DISCOGS_TOKEN', help='Discogs API token')
@click.option('--image-source', type=click.Choice(['discogs', 'google', 'google-discogs']), default='google-discogs',
              help='Image source')
@click.option('--rate-limit', type=float, default=1.0, help='Delay between requests in seconds')
@click.option('--no-rate-limit', is_flag=True, help='Disable rate limiting')
@click.option('--google-min-delay', type=float, default=3.0, help='Min delay for Google requests (default: 3 sec)')
@click.option('--google-max-delay', type=float, default=8.0, help='Max delay for Google requests (default: 8 sec)')
@click.option('--folder-matching-only', type=str, default=None, help='Filter: only process folders matching this text')
def scrap_folder_images_cmd(path, force, minimum_size, dry_run, debug, discogs_token, image_source, rate_limit, no_rate_limit,
        google_min_delay, google_max_delay, folder_matching_only):
    """Download and save folder.jpg only."""
    configure_logging(debug=debug)
    
    # Validate minimum_size option
    if minimum_size and not force:
        click.echo(click.style("WARNING: --minimum-size requires --force flag (ignoring)", fg="yellow"))
        minimum_size = None
    
    if minimum_size:
        try:
            minimum_size_bytes = parse_size_string(minimum_size)
        except ValueError as e:
            click.echo(click.style(f"ERROR: {e}", fg="red", bold=True))
            return
    
    if dry_run:
        click.echo(click.style("DRY RUN MODE - Preview only, no changes will be made", fg="cyan", bold=True))
    _handle_download(path, 'get_folder_image', force, discogs_token, image_source, rate_limit, 
                    no_rate_limit, google_min_delay, google_max_delay, folder_matching_only, minimum_size)


@cli.command('scrap_songs', cls=CustomCommand)
@click.argument('path', type=click.Path(exists=True, path_type=Path))
@click.option('--force', is_flag=True, help='Force overwrite existing metadata')
@click.option('--minimum-size', type=str, default=None, help='Only replace if new image is >= this size (e.g., 25k, 1.5m). Requires --force.')
@click.option('--dry-run', is_flag=True, help='Test mode: preview what will be processed without making changes')
@click.option('--debug', is_flag=True, help='Enable verbose debug logging')
@click.option('--discogs-token', envvar='DISCOGS_TOKEN', help='Discogs API token')
@click.option('--image-source', type=click.Choice(['discogs', 'google', 'google-discogs']), default='google-discogs',
              help='Image source')
@click.option('--rate-limit', type=float, default=1.0, help='Delay between requests in seconds')
@click.option('--no-rate-limit', is_flag=True, help='Disable rate limiting')
@click.option('--google-min-delay', type=float, default=3.0, help='Min delay for Google requests (default: 3 sec)')
@click.option('--google-max-delay', type=float, default=8.0, help='Max delay for Google requests (default: 8 sec)')
@click.option('--folder-matching-only', type=str, default=None, help='Filter: only process folders matching this text')
def scrap_song_images_cmd(path, force, minimum_size, dry_run, debug, discogs_token, image_source, rate_limit, no_rate_limit,
        google_min_delay, google_max_delay, folder_matching_only):
    """Download and embed images in songs only."""
    configure_logging(debug=debug)
    
    # Validate minimum_size option
    if minimum_size and not force:
        click.echo(click.style("WARNING: --minimum-size requires --force flag (ignoring)", fg="yellow"))
        minimum_size = None
    
    if minimum_size:
        try:
            minimum_size_bytes = parse_size_string(minimum_size)
        except ValueError as e:
            click.echo(click.style(f"ERROR: {e}", fg="red", bold=True))
            return
    
    if dry_run:
        click.echo(click.style("DRY RUN MODE - Preview only, no changes will be made", fg="cyan", bold=True))
    _handle_download(path, 'get_song_image', force, discogs_token, image_source, rate_limit, 
                    no_rate_limit, google_min_delay, google_max_delay, folder_matching_only, minimum_size)


# Aliases for backward compatibility (hidden from help)
@cli.command('scrap_folder_images', cls=CustomCommand, hidden=True)
@click.argument('path', type=click.Path(exists=True, path_type=Path))
@click.option('--force', is_flag=True, help='Force overwrite existing images')
@click.option('--minimum-size', type=str, default=None, help='Only replace if new image is >= this size (e.g., 25k, 1.5m). Requires --force.')
@click.option('--dry-run', is_flag=True, help='Test mode: preview what will be processed without making changes')
@click.option('--debug', is_flag=True, help='Enable verbose debug logging')
@click.option('--discogs-token', envvar='DISCOGS_TOKEN', help='Discogs API token')
@click.option('--image-source', type=click.Choice(['discogs', 'google', 'google-discogs']), default='google-discogs',
              help='Image source')
@click.option('--rate-limit', type=float, default=1.0, help='Delay between requests in seconds')
@click.option('--no-rate-limit', is_flag=True, help='Disable rate limiting')
@click.option('--google-min-delay', type=float, default=1.0, help='Min delay for Google requests')
@click.option('--google-max-delay', type=float, default=5.0, help='Max delay for Google requests')
@click.option('--folder-matching-only', type=str, default=None, help='Filter: only process folders matching this text')
def scrap_folder_images_alias_cmd(path, force, minimum_size, dry_run, debug, discogs_token, image_source, rate_limit, no_rate_limit,
        google_min_delay, google_max_delay, folder_matching_only):
    """Alias for scrap_folders (deprecated, use scrap_folders instead)."""
    return scrap_folder_images_cmd(path, force, minimum_size, dry_run, debug, discogs_token, image_source, rate_limit, no_rate_limit,
        google_min_delay, google_max_delay, folder_matching_only)


@cli.command('scrap_song_images', cls=CustomCommand, hidden=True)
@click.argument('path', type=click.Path(exists=True, path_type=Path))
@click.option('--force', is_flag=True, help='Force overwrite existing metadata')
@click.option('--minimum-size', type=str, default=None, help='Only replace if new image is >= this size (e.g., 25k, 1.5m). Requires --force.')
@click.option('--dry-run', is_flag=True, help='Test mode: preview what will be processed without making changes')
@click.option('--debug', is_flag=True, help='Enable verbose debug logging')
@click.option('--discogs-token', envvar='DISCOGS_TOKEN', help='Discogs API token')
@click.option('--image-source', type=click.Choice(['discogs', 'google', 'google-discogs']), default='google-discogs',
              help='Image source')
@click.option('--rate-limit', type=float, default=1.0, help='Delay between requests in seconds')
@click.option('--no-rate-limit', is_flag=True, help='Disable rate limiting')
@click.option('--google-min-delay', type=float, default=1.0, help='Min delay for Google requests')
@click.option('--google-max-delay', type=float, default=5.0, help='Max delay for Google requests')
@click.option('--folder-matching-only', type=str, default=None, help='Filter: only process folders matching this text')
def scrap_song_images_alias_cmd(path, force, minimum_size, dry_run, debug, discogs_token, image_source, rate_limit, no_rate_limit,
        google_min_delay, google_max_delay, folder_matching_only):
    """Alias for scrap_songs (deprecated, use scrap_songs instead)."""
    return scrap_song_images_cmd(path, force, minimum_size, dry_run, debug, discogs_token, image_source, rate_limit, no_rate_limit,
        google_min_delay, google_max_delay, folder_matching_only)


@cli.command('scrap', cls=CustomCommand)
@click.argument('path', type=click.Path(exists=True, path_type=Path), required=False)
@click.option('--force', is_flag=True, help='Force overwrite existing images/metadata')
@click.option('--dry-run', is_flag=True, help='Test mode: preview what will be processed without making changes')
@click.option('--debug', is_flag=True, help='Enable verbose debug logging')
@click.option('--discogs-token', envvar='DISCOGS_TOKEN', help='Discogs API token')
@click.option('--image-source', type=click.Choice(['discogs', 'google', 'google-discogs']), default='google-discogs',
              help='Image source')
@click.option('--rate-limit', type=float, default=1.0, help='Delay between requests in seconds')
@click.option('--no-rate-limit', is_flag=True, help='Disable rate limiting')
@click.option('--google-min-delay', type=float, default=1.0, help='Min delay for Google requests')
@click.option('--google-max-delay', type=float, default=5.0, help='Max delay for Google requests')
@click.option('--folder-matching-only', type=str, default=None, help='Filter: only process folders matching this text')
@click.option('--dump-images', type=click.Path(), default=None, help='DEBUG: Dump all extracted images to directory (shows order and quality). Use with --query')
@click.option('--query', type=str, default=None, help='Search query for --dump-images mode')
def scrap_cmd(path, force, dry_run, debug, discogs_token, image_source, rate_limit, no_rate_limit,
        google_min_delay, google_max_delay, folder_matching_only, dump_images, query):
    """Download and save both folder.jpg and embedded song images.
    
    Use --dump-images <dir> --query "<search>" to debug image extraction quality and order.
    """
    configure_logging(debug=debug)
    
    # Handle --dump-images debug mode
    if dump_images:
        if not query:
            click.echo(click.style("ERROR: --query is required with --dump-images", fg="red", bold=True))
            return
        
        click.echo(click.style(f"DEBUG MODE: Dumping images for query: {query}", fg="yellow", bold=True))
        from ..infrastructure.google_image_scraper import GoogleImageScraper
        
        scraper = GoogleImageScraper(
            min_delay=google_min_delay,
            max_delay=google_max_delay,
            search_strategy='discogs' if 'discogs' in image_source else 'lastfm'
        )
        
        click.echo()
        count = scraper.dump_images_for_query(query, dump_images)
        click.echo()
        click.echo(click.style(f"Saved {count} images to: {dump_images}", fg="green", bold=True))
        return
    
    # Normal mode: require path
    if not path:
        click.echo(click.style("ERROR: path argument is required (unless using --dump-images)", fg="red", bold=True))
        return
    
    if dry_run:
        click.echo(click.style("DRY RUN MODE - Preview only, no changes will be made", fg="cyan", bold=True))
    _handle_download(path, 'both', force, discogs_token, image_source, rate_limit, 
                    no_rate_limit, google_min_delay, google_max_delay, folder_matching_only)


@cli.command('scrap_string', cls=CustomCommand, hidden=True)
@click.argument('query', type=str, required=False)
@click.option('--debug', is_flag=True, help='Enable verbose debug logging')
@click.option('--output', type=click.Path(), help='Save found image to this file')
@click.option('--output-response', type=click.Path(), help='Save raw HTML response for debugging')
@click.option('--rate-limit', type=float, default=1.0, help='Delay between requests in seconds')
@click.option('--no-rate-limit', is_flag=True, help='Disable rate limiting')
@click.option('--google-min-delay', type=float, default=3.0, help='Min delay for Google requests (default: 3 sec)')
@click.option('--google-max-delay', type=float, default=8.0, help='Max delay for Google requests (default: 8 sec)')
@click.option('--dump-images', type=click.Path(), default=None, help='DEBUG: Dump all extracted images to directory')
def scrap_string_cmd(query, debug, output, output_response, rate_limit, no_rate_limit,
                     google_min_delay, google_max_delay, dump_images):
    """Search for images using a direct query string (for debugging/testing).
    
    \b
    EXAMPLES
    --------
    poetry run musee scrap_string "The Beatles Abbey Road album"
    poetry run musee scrap_string "Pink Floyd The Wall" --output album.jpg
    poetry run musee scrap_string "Nirvana Nevermind" --output-response debug.html --debug
    poetry run musee scrap_string "Album Name" --dump-images ./dumped-images --debug
    """
    configure_logging(debug=debug)
    
    # Handle --dump-images debug mode
    if dump_images:
        if not query:
            click.echo(click.style("ERROR: query argument is required with --dump-images", fg="red", bold=True))
            return
        
        click.echo(click.style(f"DEBUG MODE: Dumping images for query: {query}", fg="yellow", bold=True))
        from ..infrastructure.google_image_scraper import GoogleImageScraper
        
        scraper = GoogleImageScraper(
            min_delay=google_min_delay,
            max_delay=google_max_delay
        )
        
        click.echo()
        count = scraper.dump_images_for_query(query, dump_images)
        click.echo()
        click.echo(click.style(f"Saved {count} images to: {dump_images}", fg="green", bold=True))
        
        # Save HTML response if requested
        if output_response:
            html_response = scraper._last_response_html if hasattr(scraper, '_last_response_html') else ""
            if html_response:
                with open(output_response, 'w', encoding='utf-8') as f:
                    f.write(html_response)
                click.echo(click.style(f"HTML response saved to: {output_response}", fg="green"))
            else:
                click.echo(click.style(f"No HTML response available to save", fg="yellow"))
        
        click.echo()
        return
    
    # Normal mode
    if not query:
        click.echo(click.style("ERROR: query argument is required", fg="red", bold=True))
        return
    
    from io import BytesIO
    from PIL import Image as PILImage
    
    click.echo()
    click.echo("=" * 80)
    click.echo(click.style("MUSEE - Direct Query Search", fg="cyan", bold=True))
    click.echo("=" * 80)
    click.echo()
    click.echo(f"Query: {query}")
    click.echo()
    
    try:
        # Use GoogleImageScraper to search and select best image
        from ..infrastructure.google_image_scraper import GoogleImageScraper
        
        scraper = GoogleImageScraper(
            min_delay=google_min_delay,
            max_delay=google_max_delay
        )
        
        click.echo("Searching for images...")
        
        # Search for images using the query
        image_urls = scraper._search_google_images(query, max_results=50, use_discogs_only=False)
        
        if not image_urls:
            click.echo(click.style("✗ No images found for query", fg="red", bold=True))
            click.echo()
            return
        
        click.echo(f"Found {len(image_urls)} images. Selecting best candidate...")
        click.echo()
        
        # Try to select best image (two-pass strategy: first >=50x50, then >=20x20)
        image_data = None
        selected_url = None
        
        # Pass 1: Try images >= 50x50 pixels (preferred quality)
        for image_url in image_urls:
            img_data = scraper._download_image(image_url, min_size=50)
            if img_data:
                image_data = img_data
                selected_url = image_url
                break
        
        # Pass 2: If Pass 1 found nothing, try >= 20x20 pixels (fallback)
        if not image_data:
            for image_url in image_urls:
                img_data = scraper._download_image(image_url, min_size=20)
                if img_data:
                    image_data = img_data
                    selected_url = image_url
                    break
        
        if image_data:
            # Get image info
            try:
                img = PILImage.open(BytesIO(image_data.data))
                width, height = img.size
                size_kb = len(image_data.data) / 1024
                img_format = img.format or "unknown"
                
                # Determine quality tier
                quality_tier = "small"
                if width >= 300 and height >= 300 and size_kb >= 50:
                    quality_tier = "large"
                elif width >= 100 and height >= 100 and size_kb >= 10:
                    quality_tier = "medium"
                
                click.echo("=" * 80)
                click.echo(click.style("✓ IMAGE SELECTED", fg="green", bold=True))
                click.echo("=" * 80)
                click.echo(f"  Dimensions: {width}x{height} pixels ({quality_tier})")
                click.echo(f"  Size: {size_kb:.1f} KB")
                click.echo(f"  Format: {img_format}")
                url_display = selected_url[:75] + "..." if len(selected_url) > 75 else selected_url
                click.echo(f"  Source: {url_display}")
                click.echo()
                
                # Save image if output specified
                if output:
                    with open(output, 'wb') as f:
                        f.write(image_data.data)
                    click.echo(click.style(f"✓ Image saved to: {output}", fg="green", bold=True))
                    click.echo()
            except Exception as e:
                click.echo(click.style(f"ERROR processing image: {e}", fg="red"), err=True)
                raise
        else:
            click.echo(click.style("✗ No suitable image found (all images too small or corrupted)", fg="red", bold=True))
            click.echo()
        
        # Save HTML response if requested (for debugging)
        if output_response:
            html_response = scraper._last_response_html if hasattr(scraper, '_last_response_html') else ""
            if html_response:
                with open(output_response, 'w', encoding='utf-8') as f:
                    f.write(html_response)
                click.echo(f"HTML response saved to: {output_response}")
                click.echo()
            
    except Exception as e:
        click.echo(click.style(f"ERROR: {e}", fg="red"), err=True)
        import traceback
        if debug:
            traceback.print_exc()
        raise click.Abort()



def _handle_download(path, action, force, discogs_token, image_source, rate_limit, 
                     no_rate_limit, google_min_delay, google_max_delay, folder_matching_only=None, minimum_size=None):
    """Download images."""
    _print_header(path, action, force)
    
    effective_rate_limit = 0 if no_rate_limit else rate_limit
    click.echo(f">> Rate limiting: {effective_rate_limit}s")
    if folder_matching_only:
        click.echo(f">> Folder filter: Only process folders containing '{folder_matching_only}'")
    if minimum_size:
        click.echo(f">> Minimum image size: {minimum_size} (only replace if new image is larger)")
    click.echo()
    
    # Check for interrupted run state BEFORE showing other messages
    from ..infrastructure.state_manager import StateManager
    state_manager = StateManager()
    
    # Map action to state file action
    if action == 'get_folder_image':
        state_action = 'get_folder_image'
    elif action == 'get_song_image':
        state_action = 'get_song_image'
    elif action == 'both':
        state_action = 'both'
    else:
        state_action = action
    
    existing_state = state_manager.load_run_state(str(path), state_action)
    
    if existing_state and existing_state.status == 'in_progress':
        click.echo()
        click.secho("[RESUME] RESUMING INTERRUPTED RUN", fg="yellow", bold=True)
        
        # Show what type of items we're processing
        if state_action == 'get_folder_image':
            item_type = "folders"
        elif state_action == 'get_song_image':
            item_type = "songs"
        else:
            item_type = "items"
        
        click.echo(f"   Processed: {existing_state.processed_items}/{existing_state.total_items} {item_type}")
        click.echo(f"   Last item: {existing_state.last_processed_item}")
        click.echo(f"   To start fresh, delete: {state_manager.get_run_state_file_path()}")
        click.echo()
    
    if image_source == 'google':
        click.echo(f">> Image source: Google Images")
        image_repo = ImageRepository()
        google_scraper = GoogleImageScraper(min_delay=google_min_delay, max_delay=google_max_delay)
        image_source_adapter = GoogleImageSourceAdapter(google_scraper)
    elif image_source == 'google-discogs':
        click.echo(f">> Image source: Google Images + Discogs")
        image_repo = ImageRepository()
        google_scraper = GoogleImageScraper(min_delay=google_min_delay, max_delay=google_max_delay, search_strategy='discogs')
        image_source_adapter = GoogleImageSourceAdapter(google_scraper)
    else:
        click.echo(f">> Image source: Discogs API")
        if not discogs_token:
            click.echo("ERROR: DISCOGS_TOKEN required", err=True)
            return
        
        try:
            discogs_repo = SimpleDiscogsRepository(token=discogs_token, rate_limit_delay=effective_rate_limit)
        except ValueError as e:
            click.echo(f"ERROR: {e}", err=True)
            raise click.Abort()
        
        image_repo = ImageRepository()
        image_source_adapter = DiscogsImageSourceAdapter(discogs_repo, image_repo)
    
    service = MusicImageService(image_source_adapter, image_repo)
    service.set_progress_callback(_print_progress)
    
    folder_stats = {}
    song_stats = {}
    
    try:
        click.echo(">> Scanning directory structure (please be patient, it may take a while)...")
        click.echo()
        _print_progress_header()
        if action == 'get_folder_image':
            folder_stats = service.process_folder_images(path, force, folder_filter=folder_matching_only, minimum_size=minimum_size)
        elif action == 'get_song_image':
            song_stats = service.process_song_images(path, force, folder_filter=folder_matching_only, minimum_size=minimum_size)
        else:
            folder_stats = service.process_folder_images(path, force, folder_filter=folder_matching_only, minimum_size=minimum_size)
            song_stats = service.process_song_images(path, force, folder_filter=folder_matching_only, minimum_size=minimum_size)
        
        click.echo()
        click.echo("=" * 80)
        click.echo(click.style("SUMMARY", fg="cyan", bold=True))
        click.echo("=" * 80)
        
        if folder_stats:
            folders_processed = folder_stats.get('total_folders', 0)
            images_downloaded = folder_stats.get('images_downloaded', 0)
            skipped = folder_stats.get('skipped', 0)
            not_found = folder_stats.get('not_found', 0)
            blocked = folder_stats.get('blocked', 0)
            click.echo(f"[FOLDERS]          {folders_processed} total")
            click.echo(f"                   [OK]     {images_downloaded} images downloaded")
            if skipped > 0:
                click.echo(f"                   [SKIP]   {skipped} skipped (already have folder.jpg)")
            if not_found > 0:
                click.echo(f"                   [NONE]   {not_found} not found (no image available)")
            if blocked > 0:
                click.echo(f"                   [BLOCKED] {blocked} blocked by Google (will retry next run)")
        
        if song_stats:
            songs_processed = song_stats.get('total_songs', 0)
            images_embedded = song_stats.get('images_embedded', 0)
            skipped = song_stats.get('skipped', 0)
            not_found = song_stats.get('not_found', 0)
            blocked = song_stats.get('blocked', 0)
            click.echo(f"[SONGS]            {songs_processed} total")
            click.echo(f"                   [OK]     {images_embedded} images embedded in metadata")
            if skipped > 0:
                click.echo(f"                   [SKIP]   {skipped} skipped (already have embedded image)")
            if not_found > 0:
                click.echo(f"                   [NONE]   {not_found} not found (no image available)")
            if blocked > 0:
                click.echo(f"                   [BLOCKED] {blocked} blocked by Google (will retry next run)")
        
        click.echo()
        click.echo("=" * 80)
        click.echo(click.style("SUCCESS!", fg="green", bold=True))
        click.echo("=" * 80)
    except KeyboardInterrupt:
        click.echo()
        click.echo()
        click.secho("⏸️  INTERRUPTED! Progress has been saved.", fg="yellow", bold=True)
        click.echo(f"   Run the same command again to resume from where you left off.")
        click.echo(f"   State file: {state_manager.get_run_state_file_path()}")
        click.echo()
        raise click.Abort()
    except Exception as e:
        click.echo()
        click.echo("=" * 80)
        click.echo(click.style(f"ERROR: {e}", fg="red", bold=True))
        click.echo("=" * 80)
        raise click.Abort()


def _handle_undo(path, dry_run, image_type):
    """Undo MUSEE downloads."""
    click.echo()
    click.echo("=" * 80)
    click.echo(click.style("MUSEE - Undo Downloads", fg="cyan", bold=True))
    click.echo("=" * 80)
    click.echo()
    
    click.echo(f">> Directory: {path}")
    click.echo(f">> Mode: {'DRY RUN' if dry_run else 'ACTUAL'}")
    click.echo()
    
    undo_service = UndoService()
    
    try:
        if image_type in ['folder', 'both']:
            click.echo(">> Processing album images...")
            stats = undo_service.revert_folder_images(path, dry_run=dry_run)
            click.echo(f"   Deleted: {stats['deleted']}, Skipped: {stats['skipped']}, Errors: {stats['errors']}")
        
        if image_type in ['song', 'both']:
            click.echo(">> Processing song images...")
            stats = undo_service.revert_song_images(path, dry_run=dry_run)
            click.echo(f"   Reverted: {stats['reverted']}, Skipped: {stats['skipped']}, Errors: {stats['errors']}")
        
        # Also remove playlists
        click.echo(">> Removing playlists...")
        from ..domain.playlist_generator import PlaylistGenerator
        playlist_gen = PlaylistGenerator(dry_run=dry_run)
        removed_playlists = playlist_gen.remove_playlists(path)
        click.echo(f"   Removed: {removed_playlists}")
        
        click.echo()
        click.echo("=" * 80)
        click.echo(click.style("COMPLETED", fg="green", bold=True))
        click.echo("=" * 80)
    except Exception as e:
        click.echo(f"ERROR: {e}", err=True)
        raise click.Abort()


def _handle_cleanup(path, dry_run, image_type):
    """Clean up all folder and embedded images (destructive action)."""
    click.echo()
    click.echo("=" * 80)
    click.echo(click.style("MUSEE - Cleanup: Remove ALL Images", fg="red", bold=True))
    click.echo("=" * 80)
    click.echo()
    
    click.echo(click.style("⚠️  WARNING: This will DELETE ALL folder.jpg and embedded images!", fg="red", bold=True))
    click.echo(click.style("This includes images NOT created by MUSEE!", fg="red", bold=True))
    click.echo()
    click.echo(f">> Directory: {path}")
    click.echo(f">> Image type: {image_type}")
    click.echo(f">> Mode: {'DRY RUN (no changes)' if dry_run else click.style('ACTUAL (will delete!)', fg="red", bold=True)}")
    click.echo()
    
    if not dry_run:
        # Confirmation dialog for destructive action
        click.echo(click.style("This action CANNOT be undone!", fg="red", bold=True))
        confirmation = click.prompt(
            click.style("Type 'yes' to confirm and delete ALL images", fg="red", bold=True),
            default="no"
        )
        if confirmation.lower() != "yes":
            click.echo(click.style("❌ Cleanup cancelled", fg="yellow"))
            return
    
    undo_service = UndoService()
    
    try:
        if image_type in ['folder', 'both']:
            click.echo(">> Removing ALL album images (folder.jpg)...")
            stats = undo_service.revert_folder_images(path, dry_run=dry_run)
            click.echo(f"   Deleted: {stats['deleted']}, Skipped: {stats['skipped']}, Errors: {stats['errors']}")
        
        if image_type in ['song', 'both']:
            click.echo(">> Removing ALL embedded song images...")
            stats = undo_service.revert_song_images(path, dry_run=dry_run)
            click.echo(f"   Cleaned: {stats['reverted']}, Skipped: {stats['skipped']}, Errors: {stats['errors']}")
        
        # Also remove playlists
        click.echo(">> Removing playlists...")
        from ..domain.playlist_generator import PlaylistGenerator
        playlist_gen = PlaylistGenerator(dry_run=dry_run)
        removed_playlists = playlist_gen.remove_playlists(path)
        click.echo(f"   Removed: {removed_playlists}")
        
        click.echo()
        if dry_run:
            click.echo(click.style("✓ DRY RUN COMPLETED - No changes made", fg="yellow", bold=True))
        else:
            click.echo(click.style("✓ CLEANUP COMPLETED - All images and playlists removed", fg="green", bold=True))
        click.echo("=" * 80)
    except Exception as e:
        click.echo(f"ERROR: {e}", err=True)
        raise click.Abort()


def _print_header(path, action, force):
    """Print header."""
    click.echo()
    click.echo("=" * 80)
    click.echo(click.style(f"MUSEE - Automated Music Art Downloader (v{__version__})", fg="cyan", bold=True))
    click.echo("Author: Oscar Alvarez | https://github.com/microciudad/musee/")
    click.echo("=" * 80)
    click.echo()
    click.echo(f">> Directory: {path}")
    click.echo(f">> Action: {action}")
    click.echo(f">> Force: {'YES' if force else 'NO'}")
    click.echo()
    click.echo(">> Starting...")
    click.echo("-" * 80)


def _print_progress(info: ProcessingInfo):
    """Print progress with detailed information."""
    click.echo(info.format_compact())


def _print_progress_header():
    """Print the progress table header."""
    # Match the alignment from format_compact()
    # Build header to align perfectly with data lines (52 chars minimum before NAME)
    header = ">> [  N/ T ]      % | TYPE                        | Folder / Artist / Album or song"
    click.echo(header)


def _extract_metadata_from_name(name: str, is_file: bool = False) -> tuple[str, str, Optional[int]]:
    """Extract artist, album/title, and year from a name string.
    
    For folders: "Artist - Album (Year)" -> (artist, album, year)
    For files: "Artist - Title.mp3" -> (artist, title, None)
    """
    import re
    from pathlib import Path
    
    # Remove file extension if it's a file
    if is_file:
        name = Path(name).stem
    
    artist = ""
    album_or_title = name
    year = None
    
    # Try to extract year from parentheses or brackets at the end
    # Handles: "Album (1984)", "Album [1984]", "Album - 1984"
    year_match = re.search(r'[\s\-]\(?(\d{4})\)?(?:\s*$|[\s\-\)\]])', name)
    if year_match:
        year = int(year_match.group(1))
        # Remove year from the name
        name = re.sub(r'[\s\-]\(?' + str(year) + r'\)?(?:\s*$|[\s\-\)\]])', '', name).strip()
    
    # Try to split by " - " to get artist and album/title
    if ' - ' in name:
        parts = name.split(' - ', 1)
        artist = parts[0].strip()
        album_or_title = parts[1].strip()
    
    return artist, album_or_title, year


@cli.command('revert_images', cls=CustomCommand)
@click.argument('path', type=click.Path(exists=True, path_type=Path))
@click.option('--dry-run', is_flag=True, help='Preview changes without modifying files')
@click.option('--debug', is_flag=True, help='Enable verbose debug logging')
@click.option('--image-type', type=click.Choice(['folder', 'song', 'both']), default='both',
              help='Type of images to remove: folder, song, or both')
def revert_images_cmd(path, dry_run, debug, image_type):
    """Remove all MUSEE-created folder.jpg and embedded images."""
    configure_logging(debug=debug)
    _handle_undo(path, dry_run, image_type)


@cli.command('cleanup_images', cls=CustomCommand)
@click.argument('path', type=click.Path(exists=True, path_type=Path))
@click.option('--dry-run', is_flag=True, help='Preview changes without modifying files')
@click.option('--debug', is_flag=True, help='Enable verbose debug logging')
@click.option('--image-type', type=click.Choice(['folder', 'song', 'both']), default='both',
              help='Type of images to remove: folder, song, or both')
def cleanup_images_cmd(path, dry_run, debug, image_type):
    """Remove ALL folder.jpg and embedded images (destructive, requires confirmation)."""
    configure_logging(debug=debug)
    _handle_cleanup(path, dry_run, image_type)


@cli.command('generate_playlists', cls=CustomCommand)
@click.argument('path', type=click.Path(exists=True, path_type=Path))
@click.option('--dry-run', is_flag=True, help='Preview changes without modifying files')
@click.option('--debug', is_flag=True, help='Enable verbose debug logging')
@click.option('--skip-single-file', is_flag=True, help='Skip creating playlists for folders with only 1 song')
def generate_playlists_cmd(path, dry_run, debug, skip_single_file):
    """Generate M3U playlists for music folder hierarchy.
    
    Creates a recursive .m3u playlist for each folder, including all songs
    from that folder and all subfolders. This allows car players and media
    devices to play any level of your music hierarchy.
    
    Examples:
    \b
      musee generate_playlists /path/to/music
      musee generate_playlists /path/to/music --dry-run
      musee generate_playlists /path/to/music --skip-single-file
      musee generate_playlists /path/to/music --debug
    """
    configure_logging(debug=debug)
    
    from ..domain.playlist_generator import PlaylistGenerator
    
    if dry_run:
        click.echo(click.style("DRY RUN MODE - Preview only, no files will be created", fg="cyan", bold=True))
    
    generator = PlaylistGenerator(dry_run=dry_run, debug=debug, skip_single_file=skip_single_file)
    count = generator.generate_playlists(path)
    
    click.echo()
    if count > 0:
        click.echo(click.style(f"✅ Successfully generated {count} playlists!", fg="green", bold=True))
    else:
        click.echo(click.style("ℹ️  No playlists created (no folders with songs found)", fg="yellow"))


@cli.command('normalize_files', cls=CustomCommand)
@click.argument('path', type=click.Path(exists=True, path_type=Path))
@click.option('--dry-run', is_flag=True, help='Preview changes without modifying files')
@click.option('--debug', is_flag=True, help='Enable verbose debug logging')
def normalize_files_cmd(path, dry_run, debug):
    """Normalize and rename music files and folders.
    
    Applies title case formatting to folder names and renames audio files to a
    standardized format. File format: Artist - Title (Year) [ExtraDetails]
    
    - Title case applied to all names (first letter of each word uppercase)
    - Album order kept only if it equals the year
    - Extra details preserved if they existed previously
    - Supports undo to revert all changes
    
    Examples:
    \b
      musee normalize_files /path/to/music
      musee normalize_files /path/to/music --dry-run
      musee normalize_files /path/to/music --debug
    """
    configure_logging(debug=debug)
    
    from ..domain.rename_service import RenameService
    
    if dry_run:
        click.echo(click.style("DRY RUN MODE - Preview only, no files will be renamed", fg="cyan", bold=True))
    
    service = RenameService(dry_run=dry_run, debug=debug)
    count = service.normalize_files(path)
    
    click.echo()
    if count > 0:
        click.echo(click.style(f"✅ Successfully normalized {count} items!", fg="green", bold=True))
    else:
        click.echo(click.style("ℹ️  No items to normalize", fg="yellow"))


@cli.command('revert_renames', cls=CustomCommand)
@click.argument('path', type=click.Path(exists=True, path_type=Path))
@click.option('--dry-run', is_flag=True, help='Preview changes without reverting files')
@click.option('--debug', is_flag=True, help='Enable verbose debug logging')
def revert_renames_cmd(path, dry_run, debug):
    """Revert all file and folder renames from the last normalization.
    
    Restores files and folders to their names before the last normalize_files operation.
    This requires that the rename history file exists.
    
    Examples:
    \b
      musee revert_renames /path/to/music
      musee revert_renames /path/to/music --dry-run
      musee revert_renames /path/to/music --debug
    """
    configure_logging(debug=debug)
    
    from ..domain.rename_service import RenameService
    
    if dry_run:
        click.echo(click.style("DRY RUN MODE - Preview only, no files will be reverted", fg="cyan", bold=True))
    
    service = RenameService(dry_run=dry_run, debug=debug)
    count = service.revert_renames(path)
    
    click.echo()
    if count > 0:
        click.echo(click.style(f"✅ Successfully reverted {count} items!", fg="green", bold=True))
    else:
        click.echo(click.style("ℹ️  No renames to revert", fg="yellow"))


if __name__ == '__main__':
    cli()
