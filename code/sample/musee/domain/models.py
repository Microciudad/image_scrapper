"""Domain models for musee."""

from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional


@dataclass
class AudioFile:
    """Representa un archivo de audio."""
    path: Path
    title: Optional[str] = None
    artist: Optional[str] = None
    album: Optional[str] = None
    has_embedded_image: bool = False


@dataclass
class Album:
    """Representa un álbum musical."""
    name: str
    artist: str
    folder_path: Path
    audio_files: List[AudioFile]
    has_folder_image: bool = False
    year: Optional[int] = None  # Año extraído del nombre de la carpeta


@dataclass
class DiscogsRelease:
    """Representa una release de Discogs."""
    id: int
    title: str
    artist: str
    year: Optional[int]
    image_url: Optional[str]
    type: str  # 'master', 'release', etc.


@dataclass
class ImageData:
    """Representa datos de una imagen."""
    url: str
    data: bytes
    format: str  # 'JPEG', 'PNG', etc.
