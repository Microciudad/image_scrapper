"""Adaptadores para diferentes repositorios de imágenes."""

from typing import Optional
import logging

from .image_source import ImageSourceRepository
from ..infrastructure.simple_discogs import SimpleDiscogsRepository
from ..infrastructure.google_image_scraper import GoogleImageScraper

logger = logging.getLogger(__name__)


class DiscogsImageSourceAdapter(ImageSourceRepository):
    """Adaptador para SimpleDiscogsRepository."""
    
    def __init__(self, discogs_repo: SimpleDiscogsRepository, image_repo):
        self.discogs_repo = discogs_repo
        self.image_repo = image_repo
    
    def search_album(self, artist: str, album: str, year: Optional[int] = None) -> Optional[bytes]:
        """Busca imagen de álbum en Discogs."""
        logger.debug(f"🔍 SEARCH ALBUM | artist='{artist}' album='{album}' year={year}")
        
        releases = self.discogs_repo.search_album(artist, album, year=year)
        logger.debug(f"📊 SEARCH RESULTS | {len(releases)} releases encontradas")
        
        if not releases:
            logger.warning(f"❌ NO RESULTS | No se encontraron releases para {artist} - {album}")
            return None
        
        # Mostrar todas las opciones encontradas
        for idx, release in enumerate(releases, 1):
            image_status = "✓ CON IMAGEN" if release.image_url else "✗ SIN IMAGEN"
            logger.debug(f"  [{idx}] {image_status} | {release.title} ({release.year}) | URL: {release.image_url}")
        
        # Buscar primera release con imagen
        for release in releases:
            if release.image_url:
                try:
                    logger.info(f"✅ DOWNLOADING | {release.title} from {release.image_url}")
                    image_data = self.image_repo.download_image(release.image_url)
                    logger.info(f"✅ SUCCESS | Imagen descargada: {len(image_data.data)} bytes")
                    return image_data.data
                except Exception as e:
                    logger.debug(f"⚠️  DOWNLOAD ERROR | {e}")
                    continue
        
        logger.warning(f"❌ NO IMAGE | Ninguna release tenía imagen disponible")
        return None
    
    def search_single(self, artist: str, title: str) -> Optional[bytes]:
        """Busca imagen de single en Discogs."""
        logger.debug(f"🔍 SEARCH SINGLE | artist='{artist}' title='{title}'")
        
        releases = self.discogs_repo.search_single(artist, title)
        logger.debug(f"📊 SEARCH RESULTS | {len(releases)} releases encontradas")
        
        if not releases:
            logger.warning(f"❌ NO RESULTS | No se encontraron releases para {artist} - {title}")
            return None
        
        # Mostrar todas las opciones encontradas
        for idx, release in enumerate(releases, 1):
            image_status = "✓ CON IMAGEN" if release.image_url else "✗ SIN IMAGEN"
            logger.debug(f"  [{idx}] {image_status} | {release.title} ({release.year}) | URL: {release.image_url}")
        
        # Buscar primera release con imagen
        for release in releases:
            if release.image_url:
                try:
                    logger.info(f"✅ DOWNLOADING | {release.title} from {release.image_url}")
                    image_data = self.image_repo.download_image(release.image_url)
                    logger.info(f"✅ SUCCESS | Imagen descargada: {len(image_data.data)} bytes")
                    return image_data.data
                except Exception as e:
                    logger.debug(f"⚠️  DOWNLOAD ERROR | {e}")
                    continue
        
        logger.warning(f"❌ NO IMAGE | Ninguna release tenía imagen disponible")
        return None


class GoogleImageSourceAdapter(ImageSourceRepository):
    """Adaptador para GoogleImageScraper."""
    
    def __init__(self, google_scraper: GoogleImageScraper):
        self.scraper = google_scraper
    
    def search_album(self, artist: str, album: str, year: Optional[int] = None) -> Optional[bytes]:
        """Busca imagen de álbum en Google Images."""
        try:
            image_data = self.scraper.search_album(artist, album, year=year)
            return image_data.data if image_data else None
        except Exception as e:
            logger.warning(f"Error buscando imagen en Google: {e}")
            return None
    
    def search_single(self, artist: str, title: str) -> Optional[bytes]:
        """Busca imagen de single en Google Images."""
        try:
            image_data = self.scraper.search_single(artist, title)
            return image_data.data if image_data else None
        except Exception as e:
            logger.warning(f"Error buscando imagen en Google: {e}")
            return None
