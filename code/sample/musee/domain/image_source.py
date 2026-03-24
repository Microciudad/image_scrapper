"""Base interfaces para repositorios de imágenes."""

from abc import ABC, abstractmethod
from typing import Optional
from dataclasses import dataclass


@dataclass
class ImageResult:
    """Resultado de búsqueda de imagen (interfaz genérica)."""
    image_url: str
    source: str  # 'discogs' o 'google'
    title: Optional[str] = None


class ImageSourceRepository(ABC):
    """Interfaz base para repositorios de imágenes."""
    
    @abstractmethod
    def search_album(self, artist: str, album: str, year: Optional[int] = None) -> Optional[bytes]:
        """Busca imagen de álbum y retorna datos en bytes."""
        pass
    
    @abstractmethod
    def search_single(self, artist: str, title: str) -> Optional[bytes]:
        """Busca imagen de canción individual y retorna datos en bytes."""
        pass
