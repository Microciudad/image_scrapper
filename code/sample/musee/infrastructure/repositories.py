"""Infrastructure repositories for musee."""

import logging
import requests
import time
from io import BytesIO
from pathlib import Path
from typing import List, Optional, TYPE_CHECKING

import discogs_client
from PIL import Image

from ..domain.models import DiscogsRelease, ImageData

if TYPE_CHECKING:
    from ..utils.image_marker import MuseeImageMarker

logger = logging.getLogger(__name__)


class DiscogsRepository:
    """Repositorio para interactuar con la API de Discogs."""
    
    def __init__(self, token: str):
        self.client = discogs_client.Client(
            'musee/1.0',
            user_token=token
        )
        self._rate_limit_delay = 1.0  # Segundos entre requests
    
    def search_album(self, artist: str, album: str) -> List[DiscogsRelease]:
        """Busca un álbum en Discogs."""
        try:
            query = f'artist:"{artist}" release_title:"{album}"'
            results = self.client.search(query, type='master')
            
            self._rate_limit()
            
            releases = []
            for result in results[:5]:  # Limitar a 5 resultados
                try:
                    release = self._convert_to_domain_model(result)
                    if release:
                        releases.append(release)
                except Exception as e:
                    logger.warning(f"Error procesando resultado de Discogs: {e}")
            
            return releases
            
        except Exception as e:
            logger.error(f"Error buscando álbum en Discogs: {e}")
            return []
    
    def search_single(self, artist: str, title: str) -> List[DiscogsRelease]:
        """Busca un single en Discogs."""
        try:
            query = f'artist:"{artist}" title:"{title}" format:"Single"'
            results = self.client.search(query, type='release')
            
            self._rate_limit()
            
            releases = []
            for result in results[:3]:  # Limitar a 3 resultados para singles
                try:
                    release = self._convert_to_domain_model(result)
                    if release:
                        releases.append(release)
                except Exception as e:
                    logger.warning(f"Error procesando resultado de Discogs: {e}")
            
            return releases
            
        except Exception as e:
            logger.error(f"Error buscando single en Discogs: {e}")
            return []
    
    def _convert_to_domain_model(self, discogs_result) -> Optional[DiscogsRelease]:
        """Convierte un resultado de Discogs a modelo del dominio."""
        try:
            # Obtener imagen principal
            image_url = None
            if hasattr(discogs_result, 'images') and discogs_result.images:
                image_url = discogs_result.images[0]['uri']
            
            # Manejar artistas múltiples
            artist_names = []
            if hasattr(discogs_result, 'artists'):
                artist_names = [artist.name for artist in discogs_result.artists]
            
            artist = ', '.join(artist_names) if artist_names else 'Unknown Artist'
            
            return DiscogsRelease(
                id=discogs_result.id,
                title=discogs_result.title,
                artist=artist,
                year=getattr(discogs_result, 'year', None),
                image_url=image_url,
                type=discogs_result.__class__.__name__.lower()
            )
            
        except Exception as e:
            logger.error(f"Error convirtiendo resultado de Discogs: {e}")
            return None
    
    def _rate_limit(self):
        """Aplica rate limiting para respetar la API de Discogs."""
        time.sleep(self._rate_limit_delay)


class ImageRepository:
    """Repositorio para manejo de imágenes."""
    
    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update({
            'User-Agent': 'musee/1.0'
        })
    
    def download_image(self, url: str) -> ImageData:
        """Descarga una imagen desde una URL."""
        try:
            response = self.session.get(url, timeout=30)
            response.raise_for_status()
            
            # Verificar que es realmente una imagen
            image = Image.open(BytesIO(response.content))
            
            return ImageData(
                url=url,
                data=response.content,
                format=image.format or 'JPEG'
            )
            
        except Exception as e:
            logger.error(f"Error descargando imagen desde {url}: {e}")
            raise
    
    def save_image(self, image_data: bytes, file_path: Path, marker: Optional['MuseeImageMarker'] = None) -> None:
        """Guarda datos de imagen en un archivo con marca invisible opcional.
        
        Args:
            image_data: Datos binarios de la imagen
            file_path: Ruta donde guardar la imagen
            marker: Marca invisible MuseeImageMarker (opcional)
        """
        try:
            # Validar y procesar imagen
            image = Image.open(BytesIO(image_data))
            
            logger.info(f"Imagen original: {image.width}x{image.height} píxeles, modo: {image.mode}")
            
            # Convertir a RGB si es necesario (para JPEG)
            if image.mode in ('RGBA', 'P'):
                image = image.convert('RGB')
            
            # Solo redimensionar si es realmente muy grande (máximo 1200x1200)
            # Mantener mejor resolución para álbumes
            max_size = 1200
            if image.width > max_size or image.height > max_size:
                original_size = (image.width, image.height)
                image.thumbnail((max_size, max_size), Image.Resampling.LANCZOS)
                logger.info(f"Imagen redimensionada de {original_size} a {image.width}x{image.height}")
            else:
                logger.info(f"Manteniendo tamaño original: {image.width}x{image.height}")
            
            # Guardar como JPEG con alta calidad
            image.save(file_path, 'JPEG', quality=95, optimize=True, progressive=True)
            
            # Embeber marca invisible si se proporciona
            if marker:
                try:
                    from ..utils.image_marker import embed_image_marker
                    embed_image_marker(file_path, marker)
                except Exception as e:
                    logger.warning(f"Error al embeber marca invisible: {e}")
                    # No fallar si la marca no se puede embeber
            
            # Verificar tamaño del archivo guardado
            file_size = file_path.stat().st_size
            logger.info(f"Imagen guardada: {file_path} ({file_size} bytes, {image.width}x{image.height})")
            
        except Exception as e:
            logger.error(f"Error guardando imagen en {file_path}: {e}")
            raise
