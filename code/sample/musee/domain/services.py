"""Domain services for musee."""

import logging
import random
import re
from pathlib import Path
from typing import Callable, List, Optional, Union

from .models import Album, AudioFile, DiscogsRelease, ImageData
from .image_source import ImageSourceRepository
from ..infrastructure.repositories import DiscogsRepository, ImageRepository
from ..utils.audio_scanner import AudioScanner
from ..utils.metadata_writer import MetadataWriter
from ..utils.image_marker import MuseeImageMarker, embed_image_marker

logger = logging.getLogger(__name__)


class MusicImageService:
    """Servicio principal para manejo de imágenes de música."""

    AUDIO_EXTENSIONS = {'.mp3', '.flac', '.wav', '.ogg'}

    def __init__(self, image_source: ImageSourceRepository, image_repo: ImageRepository):
        self.image_source = image_source
        self.image_repo = image_repo
        self.audio_scanner = AudioScanner()
        self.metadata_writer = MetadataWriter()
        # Callback puede aceptar ProcessingInfo o los argumentos antiguos (Path, int, int)
        self.progress_callback: Optional[Callable] = None
        
        # Mantener referencia a discogs_repo si está disponible (para compatibilidad)
        self.discogs_repo = getattr(image_source, 'discogs_repo', None)

    def set_progress_callback(self, callback: Callable) -> None:
        """Configura un callback para mostrar progreso del procesamiento.
        
        El callback puede ser:
        - Callable[[ProcessingInfo], None] para nueva interfaz
        - Callable[[Path, int, int], None] para compatibilidad hacia atrás
        """
        self.progress_callback = callback

    def _call_progress_callback(self, file_path: Path, current: int, total: int, 
                               item_type: str, artist: Optional[str] = None, 
                               title: Optional[str] = None, album: Optional[str] = None,
                               album_type: Optional[str] = None, version: Optional[str] = None,
                               status: Optional[str] = None) -> None:
        """Llama al callback de progreso con información detallada o compatibilidad hacia atrás."""
        if not self.progress_callback:
            return
        
        # Intentar usar la nueva interfaz con ProcessingInfo
        try:
            from ..cli.main import ProcessingInfo
            info = ProcessingInfo(
                file_path=file_path,
                current=current,
                total=total,
                item_type=item_type,
                artist=artist,
                title=title,
                album=album,
                album_type=album_type,
                version=version,
                status=status
            )
            self.progress_callback(info)
        except (ImportError, TypeError):
            # Fallback a la interfaz antigua si no está disponible
            self.progress_callback(file_path, current, total)

    def process_folder_images(self, root_path: Path, force: bool = False, folder_filter: str = None, minimum_size: str = None) -> dict:
        """Procesa carpetas para descargar imágenes de álbumes.
        
        Args:
            root_path: Ruta raíz a procesar
            force: Forzar sobrescritura de imágenes existentes
            folder_filter: Filtro opcional - solo procesar carpetas dentro de carpetas que contengan este string (case-insensitive)
                          Busca recursivamente dentro de la carpeta coincidente.
            minimum_size: Tamaño mínimo para reemplazar (ej: "25k", "1.5m"). Solo con --force.
        
        Returns:
            dict: Estadísticas con claves 'total_folders', 'images_downloaded', 'skipped', 'not_found'
        """
        from ..infrastructure.state_manager import StateManager, ProcessingRunState
        
        logger.info(f"Iniciando procesamiento de carpetas en: {root_path}")
        
        # Gestión de estado para resume
        state_manager = StateManager()
        existing_state = state_manager.load_run_state(str(root_path), 'get_folder_image')
        
        try:
            # Si hay filtro, encontrar la carpeta coincidente y usarla como raíz
            search_root = root_path
            if folder_filter:
                logger.info(f"Filtro aplicado: buscando carpeta que contenga '{folder_filter}'...")
                # Buscar la carpeta que coincida con el filtro
                matching_folder = self._find_matching_folder(root_path, folder_filter)
                if matching_folder:
                    search_root = matching_folder
                    logger.info(f"Carpeta coincidente encontrada: {search_root}")
                    logger.info(f"Procesando recursivamente dentro de: {search_root}")
                else:
                    logger.warning(f"No se encontró carpeta que contenga '{folder_filter}'")
                    return {
                        'total_folders': 0,
                        'images_downloaded': 0,
                        'skipped': 0,
                        'not_found': 0
                    }

            all_albums = self._discover_albums(search_root, folder_filter)
            logger.info(f"Encontrados {len(all_albums)} álbumes")
            
            # Randomize processing order to avoid Google bot detection patterns
            # This breaks sequential queries like "Vol.001" -> "Vol.002" -> "Vol.003"
            random.shuffle(all_albums)
            logger.debug("Orden de procesamiento aleatorizado para evitar detección de patrones")
            
            # Track processed albums by path (not by index) to support randomized processing
            processed_album_paths = set(existing_state.processed_item_paths) if existing_state and existing_state.processed_item_paths else set()
            
            # Mostrar mensaje de resume si hay estado previo
            start_index = 0
            if existing_state and existing_state.status == 'in_progress':
                logger.debug(f"Resuming with {len(processed_album_paths)} previously processed albums")

            images_downloaded = 0
            skipped = 0
            not_found = 0
            blocked = 0  # Google rate limit hits
            processed = 0
            
            for i, album in enumerate(all_albums, 1):
                # Skip already processed items if resuming (check by path, not index)
                album_path_str = str(album.folder_path)
                if album_path_str in processed_album_paths:
                    skipped += 1
                    continue
                
                folder_image_path = album.folder_path / 'folder.jpg'
                
                # Check if folder already has image
                if folder_image_path.exists() and not force:
                    # Show progress for skipped folders
                    skipped += 1
                    if self.progress_callback:
                        self._call_progress_callback(
                            album.folder_path, i, len(all_albums),
                            item_type='folder',
                            artist=album.artist,
                            album=album.name,
                            album_type='proper_album',
                            status='skipped'
                        )
                    logger.info(f"✅ EXISTS | Imagen de álbum ya existe: {album.name}")
                else:
                    processed += 1
                    # Mostrar progreso si hay callback configurado
                    if self.progress_callback:
                        self._call_progress_callback(
                            album.folder_path, i, len(all_albums),
                            item_type='folder',
                            artist=album.artist,
                            album=album.name,
                            album_type='proper_album'
                        )
                    
                    try:
                        if self._process_album_image(album, force, minimum_size):
                            images_downloaded += 1
                        else:
                            not_found += 1
                        # Mark as processed even if it failed (to track it was attempted)
                        processed_album_paths.add(album_path_str)
                    except Exception as e:
                        error_msg = str(e)
                        # Track rate limits separately from "not found"
                        if "429" in error_msg or "too many" in error_msg.lower() or "/sorry" in error_msg:
                            blocked += 1  # Rate limit hit - will be retried automatically next run
                        else:
                            logger.error(f"Error procesando álbum {album.name}: {e}")
                            not_found += 1
                
                # Guardar progreso cada 10 carpetas
                if i % 10 == 0 or i == len(all_albums):
                    run_state = ProcessingRunState(
                        path=str(root_path),
                        action='get_folder_image',
                        total_items=len(all_albums),
                        processed_items=len(processed_album_paths),
                        last_processed_item=album.name,
                        status='in_progress' if len(processed_album_paths) < len(all_albums) else 'completed',
                        processed_item_paths=list(processed_album_paths)
                    )
                    state_manager.save_run_state(run_state)
            
            # Only delete state if we completed successfully
            state_manager.delete_run_state()
            
            return {
                'total_folders': len(all_albums),
                'images_downloaded': images_downloaded,
                'skipped': skipped,
                'not_found': not_found,
                'blocked': blocked
            }
        except KeyboardInterrupt:
            # Don't delete state on interrupt - user may want to resume
            logger.warning("Procesamiento interrumpido por usuario. Estado guardado para reanudar.")
            raise
        except Exception as e:
            # Don't delete state on error - user may want to resume
            logger.error(f"Error durante procesamiento: {e}")
            raise

    def process_song_images(self, root_path: Path, force: bool = False, folder_filter: str = None, minimum_size: str = None) -> dict:
        """Procesa archivos de audio para agregar imágenes a metadatos.
        
        Args:
            root_path: Ruta raíz a procesar
            force: Forzar sobrescritura de imágenes existentes
            folder_filter: Filtro opcional - procesar recursivamente dentro de la carpeta que contenga este string
            minimum_size: Tamaño mínimo para reemplazar (ej: "25k", "1.5m"). Solo con --force.
        
        Returns:
            dict: Estadísticas con claves 'total_songs', 'images_embedded', 'skipped', 'not_found'
        """
        from ..infrastructure.state_manager import StateManager, ProcessingRunState
        
        logger.info(f"Iniciando procesamiento de canciones en: {root_path}")
        if folder_filter:
            logger.info(f"Filtro aplicado: procesando recursivamente dentro de carpeta que contenga '{folder_filter}'")

        # Gestión de estado para resume
        state_manager = StateManager()
        existing_state = state_manager.load_run_state(str(root_path), 'get_song_image')

        try:
            # Si hay filtro, encontrar la carpeta coincidente y usarla como raíz
            search_root = root_path
            if folder_filter:
                matching_folder = self._find_matching_folder(root_path, folder_filter)
                if matching_folder:
                    search_root = matching_folder
                    logger.info(f"Carpeta coincidente encontrada: {search_root}")
                else:
                    logger.info(f"No se encontró carpeta que contenga '{folder_filter}'")
                    return {
                        'total_songs': 0,
                        'images_embedded': 0,
                        'skipped': 0,
                        'not_found': 0
                    }

            audio_files = self._discover_audio_files(search_root)
            logger.info(f"Encontrados {len(audio_files)} archivos de audio")

            # Randomize processing order to avoid Google bot detection patterns
            random.shuffle(audio_files)
            logger.debug("Orden de procesamiento aleatorizado para evitar detección de patrones")

            # Track processed audio files by path (not by index) to support randomized processing
            processed_file_paths = set(existing_state.processed_item_paths) if existing_state and existing_state.processed_item_paths else set()

            # Determinar si las canciones forman parte de un álbum
            albums = self._discover_albums(search_root)
            album_folders = {album.folder_path for album in albums}

            # Mostrar mensaje de resume si hay estado previo
            start_index = 0
            if existing_state and existing_state.status == 'in_progress':
                logger.debug(f"Resuming with {len(processed_file_paths)} previously processed audio files")

            images_embedded = 0
            skipped = 0
            not_found = 0
            blocked = 0  # Google rate limit hits
            
            for i, audio_file in enumerate(audio_files, 1):
                # Skip already processed items if resuming (check by path, not index)
                file_path_str = str(audio_file.path)
                if file_path_str in processed_file_paths:
                    skipped += 1
                    continue
                
                # Extraer artist/title/version PRIMERO para mostrar en progreso
                artist, title, version = self._extract_song_metadata(audio_file)
                
                # Determinar el tipo de canción
                folder = audio_file.path.parent
                if folder in album_folders:
                    album_type = 'proper_album'
                    album = next((a.name for a in albums if a.folder_path == folder), None)
                else:
                    album_type = 'loose_collection'
                    album = None
                
                # Mostrar progreso si hay callback configurado
                if self.progress_callback:
                    self._call_progress_callback(
                        audio_file.path, i, len(audio_files),
                        item_type='song',
                        artist=artist,
                        title=title,
                        album=album,
                        album_type=album_type,
                        version=version  # Pasar versión
                    )
                
                try:
                    result = self._process_song_image(audio_file, force, minimum_size)
                    if result is True:
                        images_embedded += 1
                    elif result is False:
                        # _process_song_image returns False when image not found or already embedded
                        # Check if it was skipped (already embedded) or not found
                        if audio_file.has_embedded_image and not force:
                            skipped += 1
                        else:
                            not_found += 1
                    else:
                        # None or other value means error
                        not_found += 1
                    # Mark as processed even if it failed (to track it was attempted)
                    processed_file_paths.add(file_path_str)
                except Exception as e:
                    error_msg = str(e)
                    # Track rate limits separately from "not found"
                    if "429" in error_msg or "too many" in error_msg.lower() or "/sorry" in error_msg:
                        blocked += 1  # Rate limit hit - will be retried automatically next run
                    else:
                        logger.error(f"Error procesando archivo {audio_file.path}: {e}")
                        not_found += 1
                    # Mark as processed even on error
                    processed_file_paths.add(file_path_str)
                
                # Guardar progreso cada 10 canciones
                if i % 10 == 0 or i == len(audio_files):
                    run_state = ProcessingRunState(
                        path=str(root_path),
                        action='get_song_image',
                        total_items=len(audio_files),
                        processed_items=len(processed_file_paths),
                        last_processed_item=f"{artist} - {title}",
                        status='in_progress' if len(processed_file_paths) < len(audio_files) else 'completed',
                        processed_item_paths=list(processed_file_paths)
                    )
                    state_manager.save_run_state(run_state)
            
            # Only delete state if we completed successfully
            state_manager.delete_run_state()
            
            return {
                'total_songs': len(audio_files),
                'images_embedded': images_embedded,
                'skipped': skipped,
                'not_found': not_found,
                'blocked': blocked
            }
        except KeyboardInterrupt:
            # Don't delete state on interrupt - user may want to resume
            logger.warning("Procesamiento interrumpido por usuario. Estado guardado para reanudar.")
            raise
        except Exception as e:
            # Don't delete state on error - user may want to resume
            logger.error(f"Error durante procesamiento: {e}")
            raise

    def _discover_albums(self, root_path: Path, folder_filter: str = None) -> List[Album]:
        """Descubre álbumes en la ruta especificada.
        
        Solo considera una carpeta como álbum si el artista y nombre del álbum
        pueden extraerse de forma CONFIABLE. Descarta carpetas con nombres
        genéricos (hashes, UUIDs, etc.) ya que se procesarán a nivel de canción.
        
        Si se proporciona folder_filter, permite confianza más baja para carpetas
        que coinciden con el filtro (ya que el usuario las seleccionó explícitamente).
        """
        albums = []
        
        logger.info(f"Explorando directorio: {root_path}")

        # Obtener lista de carpetas primero para calcular total
        logger.info("Enumerando carpetas (esto puede tomar un tiempo para directorios grandes)...")
        all_folders = [d for d in root_path.rglob('*') if d.is_dir()]
        total_folders = len(all_folders)
        logger.info(f"Total de carpetas encontradas: {total_folders}")
        
        # Verificar la carpeta raíz también
        folders_to_check = [root_path] + all_folders
        
        for folder_idx, folder_path in enumerate(folders_to_check, 1):
            # Mostrar progreso cada 10 carpetas o al final
            if folder_idx % 10 == 0 or folder_idx == len(folders_to_check):
                logger.info(f"Escaneando carpetas: {folder_idx}/{len(folders_to_check)} procesadas...")
                
            if not folder_path.is_dir():
                continue
                
            logger.debug(f"Revisando carpeta: {folder_path}")

            # Detectar si esta carpeta es una colección genérica (varios, varias, mixed, etc.)
            # Estas carpetas nunca son álbumes individuales - solo contienen subfolders o canciones sueltas
            various_artists_patterns = {
                'varios', 'variados', 'varias', 'various', 'va', 'v/a', 'v.a', 'var', 
                'mixed', 'mix', 'mixes', 'megamix', 'compilation', 'compilación',
                'compilations', 'recopilación', 'recopilaciones'
            }
            folder_name_lower = folder_path.name.lower()
            is_various_collection = folder_name_lower in various_artists_patterns
            
            if is_various_collection:
                logger.debug(f"Carpeta descartada (colección genérica): {folder_path.name}")
                continue  # Saltar esta carpeta - solo explorar sus subcarpetas

            audio_files = [
                self.audio_scanner.scan_file(f)
                for f in folder_path.iterdir()
                if f.suffix.lower() in self.AUDIO_EXTENSIONS
            ]
            
            logger.debug(f"Archivos de audio encontrados en {folder_path.name}: {len(audio_files)}")

            if len(audio_files) >= 1:  # Considerar álbum si tiene 1+ canciones
                album_name, album_year = self._extract_album_name(folder_path, audio_files)
                artist_name = self._extract_artist_name(audio_files, folder_path)
                
                logger.debug(f"Álbum candidato: {album_name}, Artista: {artist_name}, Año: {album_year}")

                # Detectar si esta es una colección de varios artistas (no un álbum individual)
                album_is_various = album_name and album_name.lower() in various_artists_patterns
                
                # Si el filtro coincide, usar un artista predeterminado para colecciones
                is_filter_match = folder_filter and folder_filter.lower() in folder_path.name.lower()
                if is_filter_match and not artist_name:
                    artist_name = "Various Artists"
                    logger.debug(f"Colección de filtro: asignado artista por defecto 'Various Artists'")

                if album_name and artist_name:
                    # Si es detectada como colección de varios artistas, rechazar como álbum
                    if album_is_various:
                        logger.info(
                            f"Álbum descartado (colección de varios artistas): '{album_name}'. "
                            f"Se procesarán las canciones individualmente."
                        )
                    else:
                        # Validar que la extracción sea CONFIABLE (no nombres genéricos)
                        artist_confidence = self._calculate_extraction_confidence(artist_name, is_artist=True)
                        album_confidence = self._calculate_extraction_confidence(album_name, is_artist=False)
                        
                        logger.debug(f"Confianza extracción - Artista: {artist_confidence:.1%}, Álbum: {album_confidence:.1%}")
                        
                        # Determinar umbral de confianza: más leniente si la carpeta coincide con el filtro
                        confidence_threshold = 0.0 if is_filter_match else 0.6
                        
                        # Solo crear Album si ambas extracciones están por encima del umbral
                        if artist_confidence >= confidence_threshold and album_confidence >= confidence_threshold:
                            folder_image_path = folder_path / 'folder.jpg'
                            album = Album(
                                name=album_name,
                                artist=artist_name,
                                folder_path=folder_path,
                                audio_files=audio_files,
                                has_folder_image=folder_image_path.exists()
                            )
                            # Guardar el año en un atributo temporal para usarlo en la búsqueda
                            album.year = album_year
                            albums.append(album)
                            logger.info(f"Álbum válido encontrado: {album_name} - {artist_name} ({len(audio_files)} canciones)")
                        else:
                            logger.info(
                                f"Álbum descartado (extracción no confiable): '{folder_path.name}' "
                                f"[Artista: {artist_name} ({artist_confidence:.0%}), "
                                f"Álbum: {album_name} ({album_confidence:.0%})]. "
                                f"Se procesarán las canciones individualmente."
                            )
                else:
                    logger.debug(f"Álbum descartado - falta nombre o artista: {album_name}/{artist_name}")

        return albums

    def _find_matching_folder(self, root_path: Path, filter_str: str) -> Optional[Path]:
        """Busca recursivamente la primera carpeta cuyo nombre contiene el filtro.
        
        Args:
            root_path: Ruta raíz para buscar
            filter_str: String a buscar (case-insensitive)
            
        Returns:
            Path a la carpeta coincidente, o None si no se encuentra
        """
        # Primero verificar si la carpeta raíz coincide
        if filter_str.lower() in root_path.name.lower():
            logger.debug(f"Carpeta coincidente encontrada: {root_path}")
            return root_path
        
        # Búsqueda recursiva profunda a través de todos los directorios
        try:
            for item in root_path.rglob('*'):
                if item.is_dir() and filter_str.lower() in item.name.lower():
                    logger.debug(f"Carpeta coincidente encontrada: {item}")
                    return item
        except (PermissionError, OSError) as e:
            logger.debug(f"Error buscando carpetas coincidentes: {e}")
        
        return None

    def _discover_audio_files(self, root_path: Path) -> List[AudioFile]:
        """Descubre archivos de audio en la ruta especificada."""
        audio_files = []

        for file_path in root_path.rglob('*'):
            if file_path.suffix.lower() in self.AUDIO_EXTENSIONS:
                audio_file = self.audio_scanner.scan_file(file_path)
                audio_files.append(audio_file)

        return audio_files

    def _process_album_image(self, album: Album, force: bool, minimum_size: str = None) -> bool:
        """Procesa la imagen de un álbum específico.
        
        Args:
            album: Album object to process
            force: Whether to force overwrite existing images
            minimum_size: Minimum size requirement (e.g., "25k", "1.5m")
        
        Returns:
            bool: True si la imagen fue descargada, False en caso contrario
        """
        folder_image_path = album.folder_path / 'folder.jpg'
        
        # Get size of existing image in bytes
        existing_size = None
        if folder_image_path.exists():
            existing_size = folder_image_path.stat().st_size
            
            if not force:
                logger.info(f"✅ EXISTS | Imagen de álbum ya existe: {album.name}")
                return False
            
            # If minimum_size is specified and we have existing image, check size
            if minimum_size:
                from ..cli.main import parse_size_string
                try:
                    min_size_bytes = parse_size_string(minimum_size)
                except ValueError as e:
                    logger.error(f"Invalid minimum_size format: {e}")
                    return False

        # Buscar en la fuente de imágenes configurada (Discogs o Google)
        logger.info(f"💿 ALBUM SEARCH | Artist: {album.artist} | Album: {album.name}")
        image_data = self.image_source.search_album(album.artist, album.name, year=album.year)

        if not image_data:
            logger.warning(f"❌ NO IMAGE | No image found for: {album.artist} - {album.name}")
            return False

        try:
            # image_data is an ImageData object, extract the bytes
            image_bytes = image_data.data if hasattr(image_data, 'data') else image_data
            new_size = len(image_bytes)
            
            # Check minimum_size requirement if set
            if minimum_size and existing_size:
                from ..cli.main import parse_size_string
                min_size_bytes = parse_size_string(minimum_size)
                
                if new_size < min_size_bytes:
                    logger.info(f"⏭️  SKIP | New image ({new_size} bytes) is smaller than minimum ({min_size_bytes} bytes)")
                    return False
                
                if new_size <= existing_size:
                    logger.info(f"⏭️  SKIP | New image ({new_size} bytes) is not larger than existing ({existing_size} bytes)")
                    return False
            
            # Crear marca invisible para la imagen
            marker = MuseeImageMarker(
                source=self.image_source.__class__.__name__.replace("ImageSourceAdapter", "").lower(),
                artist=album.artist,
                album=album.name,
                image_type="folder"
            )
            
            self.image_repo.save_image(image_bytes, folder_image_path, marker=marker)
            
            # Log the success
            logger.info(f"✅✅✅ SAVED folder.jpg | {album.artist} - {album.name} ({new_size/1024:.1f} KB)")
            return True
        except Exception as e:
            logger.error(f"❌ SAVE ERROR | Error guardando imagen para {album.name}: {e}")
            return False

    def _process_song_image(self, audio_file: AudioFile, force: bool, minimum_size: str = None) -> bool:
        """Procesa la imagen de una canción específica.
        
        Args:
            audio_file: AudioFile object to process
            force: Whether to force overwrite existing images
            minimum_size: Minimum size requirement (e.g., "25k", "1.5m")
        
        Returns:
            bool: True si la imagen fue embebida, False en caso contrario
        """
        if audio_file.has_embedded_image and not force:
            logger.debug(f"Imagen ya embebida en: {audio_file.path.name}")
            return False
        
        # Get size of existing embedded image if any
        existing_size = None
        if audio_file.has_embedded_image:
            try:
                # Try to get the size of embedded image
                from mutagen.id3 import ID3
                audio = ID3(audio_file.path)
                if audio and 'APIC' in audio:
                    # APIC contains the image data
                    existing_size = len(audio['APIC'].data)
            except:
                pass  # If we can't get size, proceed with replacement if minimum_size set

        # Extraer metadatos de canción (incluyendo versión)
        artist, title, version = self._extract_song_metadata(audio_file)
        
        # Extraer album y año
        album = audio_file.album
        year = None
        year_match = re.search(r'\((\d{4})\)|\[(\d{4})\]', audio_file.path.parent.name)
        if year_match:
            year = int(year_match.group(1) or year_match.group(2))
        
        # Si no tenemos album, intentar extraer del nombre de carpeta
        if not album:
            # Para canciones, el álbum es el nombre de la carpeta sin año
            folder_name = audio_file.path.parent.name
            # Remover el patrón "Artist - Album" si existe
            if ' - ' in folder_name:
                album = folder_name.split(' - ', 1)[1].strip()
            else:
                album = folder_name
            # Remover año del album si existe
            album = re.sub(r'\s*\(?\[?\d{4}\)?\]?\s*$', '', album).strip()
            
            # No intentar buscar por álbumes genéricos (indicadores de colecciones sueltas)
            loose_collection_indicators = {
                'varios', 'variados', 'various', 'va', 'v/a', 'v.a', 'var',
                'mixed', 'mix', 'mixes', 'megamix', 'collection',
                'colección', 'uncategorized', 'uncategorised', 'sin categoría', 'otros', 
                'other', 'compilations', 'compilación', 'tracks', 'canciones', 'songs'
            }
            if album.lower() in loose_collection_indicators:
                album = None  # No usar este "álbum"
        
        # Validar que tenemos información suficiente
        if not artist or not title:
            logger.warning(f"❌ MISSING METADATA | No se pudo extraer artist/title para: {audio_file.path.name}")
            return False
        
        # Construir mensaje de log con versión si existe
        version_str = f" | Version: {version}" if version else ""
        logger.info(f"🎵 PROCESSING SONG | Artist: {artist} | Title: {title}{version_str}")
        
        # Buscar en la fuente de imágenes configurada (primero single, luego álbum si existe)
        logger.debug(f"🔍 SEARCH 1/2 | Searching for single: {artist} - {title}")
        image_data = self.image_source.search_single(artist, title)

        if not image_data and album:
            logger.debug(f"🔍 SEARCH 2/2 | No single found, trying album search: {artist} - {album}")
            image_data = self.image_source.search_album(artist, album, year=year)

        # If still no image found, check if the album folder has a folder.jpg
        if not image_data:
            album_folder = audio_file.path.parent
            folder_image_path = album_folder / 'folder.jpg'
            if folder_image_path.exists():
                logger.debug(f"🔍 SEARCH 3/2 | No remote image found, using folder.jpg from album folder")
                try:
                    with open(folder_image_path, 'rb') as f:
                        image_bytes = f.read()
                    from PIL import Image
                    from io import BytesIO
                    img = Image.open(BytesIO(image_bytes))
                    from ..infrastructure.google_image_scraper import ImageData
                    image_data = ImageData(
                        data=image_bytes,
                        url=str(folder_image_path),
                        width=img.size[0],
                        height=img.size[1],
                        source="folder"
                    )
                    logger.debug(f"Loaded album folder image: {img.size[0]}x{img.size[1]}")
                except Exception as e:
                    logger.debug(f"Error loading folder.jpg: {e}")
                    image_data = None

        if not image_data:
            logger.warning(f"❌ NO IMAGE | No image found for: {artist} - {title}")
            return False

        # Embeber la imagen descargada con marca invisible
        try:
            # image_data is an ImageData object, extract the bytes
            image_bytes = image_data.data if hasattr(image_data, 'data') else image_data
            new_size = len(image_bytes)
            
            # Check minimum_size requirement if set
            if minimum_size and existing_size:
                from ..cli.main import parse_size_string
                min_size_bytes = parse_size_string(minimum_size)
                
                if new_size < min_size_bytes:
                    logger.info(f"⏭️  SKIP | New image ({new_size} bytes) is smaller than minimum ({min_size_bytes} bytes)")
                    return False
                
                if new_size <= existing_size:
                    logger.info(f"⏭️  SKIP | New image ({new_size} bytes) is not larger than existing ({existing_size} bytes)")
                    return False
            
            # Crear marca invisible para la canción
            marker = MuseeImageMarker(
                source=self.image_source.__class__.__name__.replace("ImageSourceAdapter", "").lower(),
                artist=artist,
                album=album,
                title=title,
                image_type="song"
            )
            
            # Embeber imagen en metadatos con marca
            self.metadata_writer.embed_image(audio_file.path, image_bytes, marker=marker)
            # Log the success
            logger.info(f"✅✅✅ EMBEDDED metadata | {artist} - {title} ({len(image_bytes)/1024:.1f} KB)")
            return True
        except Exception as e:
            error_msg = str(e).lower()
            # Check for corruption-related errors
            if 'sync' in error_msg or 'frame' in error_msg or 'mpeg' in error_msg:
                logger.debug(f"⏭️  SKIP CORRUPTED | Skipping corrupted audio file: {audio_file.path.name}")
            else:
                logger.error(f"❌ EMBED ERROR | Error embebiendo imagen en {audio_file.path.name}: {e}")
            return False

    def _clean_track_number(self, text: str) -> str:
        """Elimina números de track al inicio del texto.
        
        Ejemplos:
        - "01 - Artist Name" -> "Artist Name"
        - "1-INFORMATION SOCIETY" -> "INFORMATION SOCIETY"
        - "22. Song Title" -> "Song Title"
        - "Artist Name" -> "Artist Name" (sin cambios)
        """
        # Remover números de track: 1-99 al inicio seguido de separadores
        # Patrones: "01 - ", "1-", "01. ", "01: ", etc
        cleaned = re.sub(r'^\d{1,2}\s*[\.\-:\s]\s*', '', text).strip()
        return cleaned

    def _extract_version_from_title(self, title: str) -> tuple[str, Optional[str]]:
        """Extrae la versión de un título de canción.
        
        La versión está entre paréntesis, corchetes o llaves.
        Ejemplos:
        - "Song Title (Remix)" -> ("Song Title", "Remix")
        - "Song Title [Radio Edit]" -> ("Song Title", "Radio Edit")
        - "Song Title {Extended}" -> ("Song Title", "Extended")
        - "Song Title" -> ("Song Title", None)
        - "Song (Demo) (Remaster)" -> ("Song", "Remaster") # Solo la última
        
        Returns:
            tuple: (clean_title, version)
        """
        if not title:
            return title, None
        
        # Buscar versión en paréntesis, corchetes o llaves AL FINAL DEL TÍTULO
        # Solo captura el último patrón
        version_match = re.search(r'\s*[\(\[\{]\s*([^\(\[\{]+?)\s*[\)\]\}]\s*$', title)
        if version_match:
            version = version_match.group(1).strip()
            clean_title = title[:version_match.start()].strip()
            return clean_title, version
        
        return title, None

    def _clean_title_for_search(self, title: str) -> str:
        """Limpia caracteres especiales de un título para mejorar búsquedas.
        
        Reemplaza separadores como "/" y "_" con espacios para que Last.fm
        pueda encontrar resultados. Ejemplos:
        - "You Are Mine / Two Hearts" -> "You Are Mine Two Hearts"
        - "Song_Title_Remix" -> "Song Title Remix"
        - "Track (Special) Edition" -> "Track Special Edition" (paréntesis removidos)
        
        Args:
            title: Título de canción a limpiar
            
        Returns:
            Título limpio listo para búsqueda
        """
        if not title:
            return title
        
        # Reemplazar guiones, barras y underscores con espacios
        cleaned = re.sub(r'[/_\-]+', ' ', title)
        
        # Remover paréntesis/corchetes/llaves innecesarios (que no son versiones)
        # Solo si no están al final (las versiones se extrajeron ya)
        cleaned = re.sub(r'\s*[\(\[\{].*?[\)\]\}]\s*', ' ', cleaned)
        
        # Limpiar espacios múltiples
        cleaned = re.sub(r'\s+', ' ', cleaned).strip()
        
        return cleaned

    def _title_case(self, text: str) -> str:
        """Convierte texto a Title Case manteniendo palabras pequeñas en minúsculas y preservando acrónimos.
        
        Ejemplos:
        - "INFORMATION SOCIETY" -> "Information Society"
        - "run dmc" -> "Run Dmc"  
        - "the beatles" -> "The Beatles"
        - "dj khaled" -> "Dj Khaled"
        """
        if not text:
            return text
        
        # Palabras que deberían estar en minúsculas en Title Case
        small_words = {'a', 'an', 'and', 'as', 'at', 'be', 'by', 'en', 'for', 
                      'if', 'in', 'is', 'it', 'of', 'on', 'or', 'to', 'the', 'y', 'o'}
        
        # Acrónimos conocidos que deben estar en mayúsculas
        acronyms = {'usa', 'uk', 'us', 'dj', 'dmc', 'tv', 'ao', 'mr', 'dr', 'jr', 'sr'}
        
        words = text.split()
        result = []
        
        for i, word in enumerate(words):
            word_lower = word.lower()
            
            # Primera palabra siempre con mayúscula
            if i == 0:
                result.append(word.capitalize())
            # Acrónimos conocidos en mayúsculas
            elif word_lower in acronyms:
                result.append(word_lower.upper())
            # Palabras pequeñas en minúsculas
            elif word_lower in small_words:
                result.append(word_lower)
            # Resto en Title Case
            else:
                result.append(word.capitalize())
        
        return ' '.join(result)

    def _extract_song_metadata(self, audio_file: AudioFile) -> tuple[Optional[str], Optional[str], Optional[str]]:
        """Extrae artista, título y versión de una canción.
        
        Retorna:
            tuple: (artist, title, version)
        """
        # Paso 1: Intentar extraer del archivo
        artist = audio_file.artist
        title = audio_file.title
        
        # Lista de nombres de carpetas que indican "canciones sueltas"
        # Si la carpeta tiene estos nombres, ignoramos extraer artista de carpeta padre
        loose_collection_indicators = {
            'varios', 'variados', 'mixed', 'mix', 'mixes', 'megamix', 'collection',
            'colección', 'uncategorized', 'uncategorised', 'sin categoría', 'otros', 
            'other', 'compilations', 'compilación', 'tracks', 'canciones', 'songs'
        }
        
        folder_name = audio_file.path.parent.name
        folder_name_lower = folder_name.lower()
        is_loose_collection_folder = folder_name_lower in loose_collection_indicators
        
        # Paso 2: Si no tiene artist/title en metadatos, intentar extraer del nombre de archivo
        if not artist or not title:
            file_stem = audio_file.path.stem
            file_artist, file_title = self._split_artist_title_from_filename(file_stem)
            
            # Usar valores del archivo solo si no los tenemos en metadatos
            if not artist and file_artist:
                artist = file_artist
            if not title and file_title:
                title = file_title
        
        # Paso 3: Si aún no tenemos artista, intentar extraer de la carpeta
        if not artist:
            # Intentar patrón "Artist - Album" en nombre de carpeta
            # Limpiar números de track si están al inicio
            folder_name_clean = self._clean_track_number(folder_name)
            
            if ' - ' in folder_name_clean:
                potential_artist = folder_name_clean.split(' - ')[0].strip()
                if len(potential_artist) > 2:
                    artist = potential_artist
            
            # Si aún no tenemos artista, intentar carpeta padre o abuelo
            # PERO: Solo si la carpeta actual NO es una carpeta de "canciones sueltas"
            if not artist and not is_loose_collection_folder:
                # Obtener los nombres de las carpetas padre y abuelo
                immediate_parent = audio_file.path.parent.name
                parent_folder = audio_file.path.parent.parent.name if audio_file.path.parent.parent != audio_file.path.parent.parent.parent else None
                grandparent_folder = audio_file.path.parent.parent.parent.name if audio_file.path.parent.parent.parent != audio_file.path.parent.parent.parent.parent else None
                
                # Caso 1: Estructura de 2 niveles: Genre/Artist/Song
                # Si la carpeta padre es un género, la carpeta actual (immediate_parent) es el artista
                if parent_folder and self._is_genre_folder(parent_folder):
                    artist = immediate_parent
                    logger.debug(f"Artista extraído de carpeta actual (género detectado en padre): {artist}")
                
                # Caso 2: Estructura de 1 nivel: Artist/Song (padre es artista)
                # Si la carpeta padre NO es un género, es el artista
                elif parent_folder and not self._is_genre_folder(parent_folder):
                    artist = parent_folder
                    logger.debug(f"Artista extraído de carpeta padre: {artist}")
                
                # Caso 3: Si nada funciona, usar el nombre de carpeta actual
                elif not parent_folder:
                    # No hay carpeta padre, usar la carpeta actual
                    artist = immediate_parent
                    logger.debug(f"Artista extraído de carpeta actual (sin padre): {artist}")
        
        # Paso 4: Extraer versión del título (lo que está entre paréntesis/corchetes/llaves)
        title_clean = title
        version = None
        if title:
            title_clean, version = self._extract_version_from_title(title)
            # Limpiar caracteres especiales de busqueda (guiones, barras, underscores)
            title_clean = self._clean_title_for_search(title_clean)
        
        # Paso 5: Aplicar Title Case al artista y título
        if artist:
            artist = self._title_case(artist)
        if title_clean:
            title_clean = self._title_case(title_clean)
        
        return artist, title_clean, version

    def _extract_album_name(self, folder_path: Path, audio_files: List[AudioFile]) -> tuple[Optional[str], Optional[int]]:
        """Extrae el nombre del álbum y el año del nombre de la carpeta o metadatos.
        
        Returns:
            tuple: (album_name, year) donde year es None si no se encuentra
        """
        # Intentar desde metadatos primero - ESTO TIENE PRIORIDAD
        albums = {af.album for af in audio_files if af.album}
        if len(albums) == 1:
            album_from_metadata = albums.pop()
            logger.debug(f"Álbum extraído de metadatos: {album_from_metadata}")
            return album_from_metadata, None

        # Usar nombre de carpeta como fallback y limpiarlo
        folder_name = folder_path.name
        
        # Limpiar números de track si están al inicio
        folder_name = self._clean_track_number(folder_name)
        
        # Extraer año si está presente: "Album (YYYY)", "Album [YYYY]", "Album - YYYY", o "Album YYYY"
        year = None
        # Search for year in various formats: (YYYY), [YYYY], - YYYY, or space YYYY at end
        year_match = re.search(r'[\s\-]\(?(\d{4})\)?(?:\s*$|[\s\-\)\]])', folder_name)
        if year_match:
            year = int(year_match.group(1))
            logger.debug(f"Año extraído de carpeta: {year}")
        
        # Limpiar sufijos comunes incluyendo el año
        cleaned_name = re.sub(r'\s*\((\d{4})\)\s*', '', folder_name)  # Remover (YYYY)
        cleaned_name = re.sub(r'\s*\[(\d{4})\]\s*', '', cleaned_name)  # Remover [YYYY]
        cleaned_name = re.sub(r'\s*-\s*(\d{4})\s*$', '', cleaned_name)  # Remover - YYYY al final
        cleaned_name = re.sub(r'\s+(\d{4})\s*$', '', cleaned_name)  # Remover YYYY al final
        cleaned_name = re.sub(r'\s*\((Album|EP|Single|Deluxe|Remaster|CD|LP)\)\s*$', '', cleaned_name, flags=re.IGNORECASE)
        cleaned_name = re.sub(r'\s*\[(Album|EP|Single|Deluxe|Remaster|CD|LP)\]\s*$', '', cleaned_name, flags=re.IGNORECASE)
        cleaned_name = cleaned_name.strip()
        
        # Si el nombre contiene " - " y parece ser patrón "Artista - Álbum", remover el prefijo de artista
        # Esto es importante para carpetas como "Various - Master Mix 2" o "Artist - Album Name"
        if ' - ' in cleaned_name:
            parts = cleaned_name.split(' - ', 1)
            potential_artist = parts[0].strip()
            potential_album = parts[1].strip() if len(parts) > 1 else cleaned_name
            
            # Si el potencial artista parece un nombre real (>2 chars) y hay un álbum después, usar solo el álbum
            if len(potential_artist) > 2 and len(potential_album) > 0:
                cleaned_name = potential_album
                logger.debug(f"Removido prefijo de artista '{potential_artist}' de '{folder_name}'")
        
        logger.debug(f"Álbum extraído del nombre de carpeta: '{folder_name}' -> '{cleaned_name}' (año: {year})")
        return (cleaned_name if cleaned_name else folder_name), year

    def _extract_artist_name(self, audio_files: List[AudioFile], folder_path: Path) -> Optional[str]:
        """Extrae el nombre del artista de los metadatos o estructura de carpetas.
        
        Cuando hay múltiples niveles de carpetas (ej: Genre -> Artist -> Album),
        intenta extraer el artista del nivel correcto, saltando carpetas de género.
        
        También ignora carpetas de metadatos como "Volume 1", "CD 3", "Part 2", etc.
        
        Estructura soportada:
        - Artist/Album (simple)
        - Artist/Year - Album (album con año)
        - Genre/Artist/Album
        - Genre/Artist/Year - Album
        - Artist/Volume 1 (ignora "Volume 1", usa Artist)
        - Artist/Volume 1/CD 1 (ignora ambas, usa Artist)
        """
        # Intentar desde metadatos primero
        artists = {af.artist for af in audio_files if af.artist}
        if len(artists) == 1:
            artist_from_metadata = artists.pop()
            logger.debug(f"Artista extraído de metadatos: {artist_from_metadata}")
            return artist_from_metadata
        
        # Si no hay metadatos o hay múltiples artistas, intentar extraer de la estructura de carpetas
        current_folder = folder_path.name
        current_folder = self._clean_track_number(current_folder)
        
        parent_folder = folder_path.parent.name if folder_path.parent != folder_path.parent.parent else None
        grandparent_folder = folder_path.parent.parent.name if folder_path.parent.parent != folder_path.parent.parent.parent else None
        
        # NUEVA LÓGICA: Si la carpeta actual es un folder de metadatos (Volume 1, CD 3, etc)
        # intentar usar la carpeta padre o más arriba
        if self._is_metadata_folder(current_folder):
            logger.debug(f"Carpeta actual es metadatos '{current_folder}', saltando...")
            
            # Intentar usar la carpeta padre si no es también metadatos
            if parent_folder and not self._is_metadata_folder(parent_folder):
                if not self._is_genre_folder(parent_folder):
                    cleaned_parent = re.sub(r'\s*(Music|Albums?|Discography|Collection)\s*$', '', parent_folder, flags=re.IGNORECASE).strip()
                    if cleaned_parent and len(cleaned_parent) > 1:
                        logger.debug(f"Artista extraído de carpeta padre (saltando metadatos): {cleaned_parent}")
                        return cleaned_parent
                elif grandparent_folder:
                    # Si padre es género, buscar en abuelo
                    cleaned_grandparent = re.sub(r'\s*(Music|Albums?|Discography|Collection)\s*$', '', grandparent_folder, flags=re.IGNORECASE).strip()
                    if cleaned_grandparent and len(cleaned_grandparent) > 1 and not self._is_genre_folder(cleaned_grandparent):
                        logger.debug(f"Padre es género, artista extraído de abuelo (saltando metadatos): {cleaned_grandparent}")
                        return cleaned_grandparent
            
            # Si padre también es metadatos (ej: Volume 1/CD 3), ir más arriba
            if parent_folder and self._is_metadata_folder(parent_folder) and grandparent_folder:
                logger.debug(f"Ambas son metadatos ({current_folder} y {parent_folder}), usando abuelo...")
                if not self._is_genre_folder(grandparent_folder):
                    cleaned_grandparent = re.sub(r'\s*(Music|Albums?|Discography|Collection)\s*$', '', grandparent_folder, flags=re.IGNORECASE).strip()
                    if cleaned_grandparent and len(cleaned_grandparent) > 1:
                        logger.debug(f"Artista extraído de abuelo: {cleaned_grandparent}")
                        return cleaned_grandparent
        
        # Verificar si la carpeta actual comienza con un año (YYYY - Album)
        current_starts_with_year = False
        if ' - ' in current_folder:
            parts = current_folder.split(' - ')
            first_part = parts[0].strip()
            if re.match(r'^\d{4}$', first_part):
                current_starts_with_year = True
                logger.debug(f"Carpeta actual comienza con año: {first_part}")
        
        # Si la carpeta actual es del tipo "YYYY - Album", intentar usar la carpeta padre como artista
        if current_starts_with_year and parent_folder:
            if not self._is_metadata_folder(parent_folder) and not self._is_genre_folder(parent_folder):
                cleaned_parent = re.sub(r'\s*(Music|Albums?|Discography|Collection)\s*$', '', parent_folder, flags=re.IGNORECASE).strip()
                if cleaned_parent and len(cleaned_parent) > 1:
                    logger.debug(f"Carpeta actual es 'YYYY - Album', artista extraído de padre: {cleaned_parent}")
                    return cleaned_parent
            elif parent_folder and self._is_metadata_folder(parent_folder) and grandparent_folder:
                # Si padre es metadatos, usar abuelo
                cleaned_grandparent = re.sub(r'\s*(Music|Albums?|Discography|Collection)\s*$', '', grandparent_folder, flags=re.IGNORECASE).strip()
                if cleaned_grandparent and len(cleaned_grandparent) > 1 and not self._is_genre_folder(cleaned_grandparent):
                    logger.debug(f"Padre es metadatos, artista extraído de abuelo: {cleaned_grandparent}")
                    return cleaned_grandparent
            elif parent_folder and self._is_genre_folder(parent_folder) and grandparent_folder:
                # Si padre es género, buscar en abuelo
                cleaned_grandparent = re.sub(r'\s*(Music|Albums?|Discography|Collection)\s*$', '', grandparent_folder, flags=re.IGNORECASE).strip()
                if cleaned_grandparent and len(cleaned_grandparent) > 1 and not self._is_genre_folder(cleaned_grandparent):
                    logger.debug(f"Padre es género, artista extraído de abuelo: {cleaned_grandparent}")
                    return cleaned_grandparent
        
        # Patrón: "Artista - Álbum" en carpeta actual
        # Pero si comienza con un año (YYYY), saltarlo: "1996 - Hans zimmer - The Rock"
        if ' - ' in current_folder:
            parts = current_folder.split(' - ')
            first_part = parts[0].strip()
            
            # Si el primer parte es un año (4 dígitos), usar el segundo parte como artista
            if re.match(r'^\d{4}$', first_part):
                if len(parts) > 1:
                    artist_candidate = parts[1].strip()
                else:
                    artist_candidate = first_part
            else:
                artist_candidate = first_part
            
            # Limpiar números de track del candidato también (por si acaso)
            artist_candidate = self._clean_track_number(artist_candidate)
            if len(artist_candidate) > 2:  # Solo aceptar si tiene más de 2 caracteres después de limpiar
                logger.debug(f"Artista extraído del patrón 'Artista - Álbum': {artist_candidate}")
                return artist_candidate
        
        # Si la carpeta padre parece ser un artista (no es una carpeta de género musical ni metadatos)
        if parent_folder and not self._is_genre_folder(parent_folder) and not self._is_metadata_folder(parent_folder):
            # Limpiar nombres comunes de carpetas padre
            cleaned_parent = re.sub(r'\s*(Music|Albums?|Discography|Collection)\s*$', '', parent_folder, flags=re.IGNORECASE).strip()
            if cleaned_parent and len(cleaned_parent) > 1:
                logger.debug(f"Artista extraído de carpeta padre: '{parent_folder}' -> '{cleaned_parent}'")
                return cleaned_parent
        
        # Si la carpeta padre ES un género, intentar con la abuela (ej: Genre/Artist/Album)
        if parent_folder and self._is_genre_folder(parent_folder) and grandparent_folder:
            logger.debug(f"Carpeta padre es género '{parent_folder}', buscando artista en abuelo")
            cleaned_grandparent = re.sub(r'\s*(Music|Albums?|Discography|Collection)\s*$', '', grandparent_folder, flags=re.IGNORECASE).strip()
            if cleaned_grandparent and len(cleaned_grandparent) > 1 and not self._is_genre_folder(cleaned_grandparent):
                logger.debug(f"Artista extraído de carpeta abuelo: '{grandparent_folder}' -> '{cleaned_grandparent}'")
                return cleaned_grandparent
        
        logger.debug("No se pudo extraer el artista")
        return None
    
    def _calculate_extraction_confidence(self, name: str, is_artist: bool = True) -> float:
        """Calcula la confianza en una extracción de nombre (artista o álbum).
        
        Retorna un valor entre 0.0 (sin confianza) y 1.0 (confianza total).
        
        Criterios:
        - Penaliza nombres genéricos (UUIDs, hashes, números)
        - Penaliza nombres muy cortos
        - Penaliza nombres con muchos números/símbolos
        - Penaliza nombres genéricos comunes ("varios", "test", "prueba", etc.)
        - Recompensa nombres de longitud normal con palabras claras
        """
        if not name or len(name.strip()) == 0:
            return 0.0
        
        name = name.strip()
        confidence = 1.0
        
        # Lista de nombres genéricos que suelen ser carpetas de prueba o no válidas
        generic_names = {
            'varios', 'variados', 'various', 'va', 'v/a', 'v.a', 'var',
            'mixed', 'test', 'prueba', 'tmp', 'temp', 
            'download', 'downloads', 'descargas', 'collection', 'colección',
            'uncategorized', 'uncategorised', 'sin categoría', 'otros', 'other',
            'music', 'musica', 'songs', 'canciones', 'tracks', 'demos', 'samples'
        }
        
        if name.lower() in generic_names:
            logger.debug(f"Nombre genérico rechazado: {name}")
            return 0.1  # Muy baja confianza
        
        # Penalizar UUIDs y hashes (patrones como "1ebd0ea6-da62-47b0..." o números largos)
        if re.match(r'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}', name, re.IGNORECASE):
            logger.debug(f"Nombre descartado (UUID): {name}")
            return 0.1  # UUID detectado
        
        # Penalizar hashes MD5/SHA (cadenas hexadecimales largas)
        if re.match(r'^[0-9a-f]{32}$|^[0-9a-f]{40}$|^[0-9a-f]{64}$', name, re.IGNORECASE):
            logger.debug(f"Nombre descartado (hash): {name}")
            return 0.1  # Hash detectado
        
        # Penalizar nombres numéricos puros o principalmente numéricos
        digit_ratio = sum(1 for c in name if c.isdigit()) / len(name)
        if digit_ratio > 0.5:
            confidence *= 0.3  # Muchos números
            logger.debug(f"Nombre principalmente numérico penalizado: {name} ({digit_ratio:.0%} dígitos)")
        
        # Penalizar nombres muy cortos (menos de 2 caracteres)
        if len(name) < 2:
            return 0.1
        
        # Penalizar nombres que parecen IDs temporales (timestamp, UUID truncado, etc)
        # Ejemplo: "1ebd0ea6-da62-47b0-9a55" -> parece truncado
        if re.search(r'_[0-9a-f]{8}-[0-9a-f]{4}', name, re.IGNORECASE):
            confidence *= 0.4
            logger.debug(f"Nombre con patrón UUID truncado penalizado: {name}")
        
        # Penalizar nombres con solo caracteres especiales/guiones
        # pero permitir algún patrón "word - word"
        special_char_count = sum(1 for c in name if not c.isalnum() and c not in [' ', '-', '&'])
        if special_char_count / len(name) > 0.3:
            confidence *= 0.5
        
        # Para artistas: preferir nombres de 2+ palabras (más confiable)
        if is_artist:
            word_count = len(name.split())
            if word_count == 1 and len(name) < 4:
                confidence *= 0.6  # Artista muy corto
            elif word_count >= 2:
                confidence *= 1.1  # Artista con múltiples palabras es más probable
        
        # Para álbumes: permitir pero preferir nombres con cierta longitud
        else:
            if len(name) < 3:
                confidence *= 0.4  # Álbum muy corto
            elif len(name) > 100:
                confidence *= 0.7  # Álbum anormalmente largo
        
        # Penalizar si tiene muchos números no relacionados con años
        year_patterns = len(re.findall(r'\b(19|20)\d{2}\b', name))
        non_year_numbers = digit_ratio - (year_patterns * 4 / len(name))
        if non_year_numbers > 0.3:
            confidence *= 0.6
        
        # Asegurar que la confianza esté dentro de rango [0.0, 1.0]
        confidence = max(0.0, min(1.0, confidence))
        
        logger.debug(f"Confianza extracción para '{name}': {confidence:.2f}")
        return confidence
    
    def _is_genre_folder(self, folder_name: str) -> bool:
        """Determina si el nombre de carpeta parece ser un género musical o categoría estilo."""
        folder_lower = folder_name.lower().strip()
        
        # Lista de géneros musicales
        genres = {
            'rap', 'hip hop', 'hip-hop', 'hiphop', 'rock', 'pop', 'jazz', 'blues', 'classical', 'country', 
            'electronic', 'house', 'techno', 'reggae', 'folk', 'metal', 'punk', 'funk', 'soul', 'r&b',
            'rnb', 'indie', 'alternative', 'disco', 'salsa', 'cumbia', 'reggaeton', 'bachata', 'merengue',
            'tango', 'bossa nova', 'latino', 'latin', 'dance', 'dubstep', 'trap', 'grime', 'garage',
            'garage rock', 'grunge', 'psych', 'psychedelic', 'prog', 'progressive', 'ambient',
            'freestyle', 'freestyle rap', 'new wave', 'synth pop', 'synth-pop', 'electro', 'electro-pop',
            'varios', 'variados', 'varias', 'mixes', 'compilations', 'soundtracks', 'mixed'
        }
        
        if folder_lower in genres:
            return True
        
        # Detectar patrones como "YEAR - GENRE" (ej: "2000s - Freestyle", "80s - Synth Pop")
        # Patrón: empieza con año/década
        if re.match(r"^(19|20)\d{2}s?\s*[-–]\s*", folder_lower):
            return True
        
        # Si contiene género entre múltiples palabras (ej: "Pop", "Rock", "Dance" después de "-")
        if ' - ' in folder_lower:
            parts = folder_lower.split(' - ', 1)
            if len(parts) > 1:
                potential_genre = parts[-1].strip()
                # Permitir que generar nombres sean tratados como géneros
                if potential_genre in genres or potential_genre in {'pop', 'rock', 'electronic', 'indie'}:
                    return True
        
        return False

    def _is_metadata_folder(self, folder_name: str) -> bool:
        """Determina si el nombre de carpeta es un folder de metadatos (Volume, Disc, CD, Part, etc).
        
        Estas carpetas contienen información del álbum físico, no del artista.
        Ejemplos: "Volume 1", "CD 3", "Disc 2", "Part 1 (2006)"
        """
        folder_lower = folder_name.lower().strip()
        
        # Patrones de carpetas de metadatos
        # Debe ser principalmente el patrón + número, sin mucho más texto
        metadata_patterns = [
            r'^volume\s+\d+',          # "Volume 1", "Volume 2"
            r'^vol\.?\s+\d+',          # "Vol. 1", "Vol 1"
            r'^disc\s+\d+',            # "Disc 1", "Disc 2"
            r'^cd\s+\d+',              # "CD 1", "CD 2"
            r'^part\s+\d+',            # "Part 1", "Part 2"
            r'^dvd\s+\d+',             # "DVD 1"
            r'^side\s+[a-z]',          # "Side A", "Side B"
            r'^tape\s+\d+',            # "Tape 1", "Tape 2"
            r'^disk\s+\d+',            # "Disk 1"
        ]
        
        for pattern in metadata_patterns:
            if re.match(pattern, folder_lower):
                logger.debug(f"Carpeta identificada como metadatos: '{folder_name}' (patrón: {pattern})")
                return True
        
        # Patrón especial: "YYYY - Something" donde solo queremos ignorar si es muy genérico
        # Ejemplo: "2006 - CD 1" o "2006 - Volume 1"
        if re.match(r"^\d{4}\s*[-–]\s*", folder_lower):
            second_part = folder_lower.split(' - ', 1)[1].strip() if ' - ' in folder_lower else ""
            for pattern in metadata_patterns:
                if re.match(pattern, second_part):
                    logger.debug(f"Carpeta con año + metadatos identificada: '{folder_name}'")
                    return True
        
        return False

    def _split_artist_title_from_filename(self, filename_stem: str) -> tuple[Optional[str], Optional[str]]:
        """Separa artista y título del nombre de un archivo de audio.
        
        Soporta varios formatos:
        - "ARTIST - TITLE" o "ARTIST - TITLE (version)"
        - "ARTIST-TITLE" (guion simple sin espacios)
        - "01-ARTIST-TITLE" (con número de track)
        - "01. ARTIST - TITLE"
        - "ARTIST TITLE" (sin separador, si tiene múltiples palabras)
        
        Args:
            filename_stem: Nombre del archivo sin extensión
            
        Returns:
            tuple: (artist, title) donde ambos pueden ser None si no se pueden extraer
        """
        if not filename_stem or len(filename_stem.strip()) < 2:
            return None, None
        
        # Remover número de track y punto/guion/espacio del inicio
        # Soporta: 01-, 1-, 01., 01 (con espacio), etc.
        cleaned = re.sub(r'^\d+[\.\-\s]+', '', filename_stem).strip()
        
        # Intentar patrón "ARTIST - TITLE" (con espacios alrededor del guion)
        if ' - ' in cleaned:
            parts = cleaned.split(' - ', 1)  # Solo dividir en el primer ' - '
            artist, title = parts[0].strip(), parts[1].strip()
            if artist and title:
                return artist, title
        
        # Intentar patrón "ARTIST-TITLE" (guion simple, sin espacios)
        # Buscar el guion que NO sea parte de un número
        if '-' in cleaned:
            # Evitar dividir si el guion está precedido por dígitos (como en "2-pack")
            parts = re.split(r'(?<!\d)-(?!\d)', cleaned, maxsplit=1)
            if len(parts) == 2:
                artist, title = parts[0].strip(), parts[1].strip()
                # Validar que ambas partes sean significativas (no solo números)
                if artist and title and len(artist) > 2 and len(title) > 2:
                    return artist, title
        
        # Si solo hay una parte (sin separador claro), asumir que es el título
        # y no hay artista disponible en el nombre del archivo
        return None, cleaned if len(cleaned) > 2 else None
