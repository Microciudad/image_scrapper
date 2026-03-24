"""Marca invisible (watermark) para imágenes descargadas por MUSEE."""

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Optional, Tuple, Dict, Any
from pathlib import Path

from PIL import Image
from PIL.Image import Image as PILImage
import json

logger = logging.getLogger(__name__)

# Versión del formato de marca
MUSEE_MARKER_VERSION = "v1"
MUSEE_MARKER_KEY = f"MUSEE_METADATA_{MUSEE_MARKER_VERSION}"


@dataclass
class MuseeImageMarker:
    """Metadata de marca invisible MUSEE."""
    version: str = MUSEE_MARKER_VERSION
    timestamp: str = None
    source: str = "unknown"  # "discogs" o "google"
    artist: Optional[str] = None
    album: Optional[str] = None
    title: Optional[str] = None  # Para canciones
    image_type: str = "folder"  # "folder" o "song"
    
    def __post_init__(self):
        if self.timestamp is None:
            self.timestamp = datetime.now().isoformat()
    
    def to_dict(self) -> Dict[str, Any]:
        """Convierte a diccionario JSON."""
        return {
            "version": self.version,
            "timestamp": self.timestamp,
            "source": self.source,
            "artist": self.artist,
            "album": self.album,
            "title": self.title,
            "image_type": self.image_type,
        }
    
    def to_json(self) -> str:
        """Convierte a JSON string."""
        return json.dumps(self.to_dict(), ensure_ascii=True)
    
    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "MuseeImageMarker":
        """Crea desde diccionario."""
        return cls(**data)
    
    @classmethod
    def from_json(cls, json_str: str) -> "MuseeImageMarker":
        """Crea desde JSON string."""
        try:
            data = json.loads(json_str)
            return cls.from_dict(data)
        except Exception as e:
            logger.error(f"Error al parsear JSON de marca: {e}")
            raise


def embed_image_marker(image_path: Path, marker: MuseeImageMarker) -> bool:
    """
    Embebe metadata en una imagen usando EXIF (JPEG) o PNG metadata.
    
    Para JPEG: Guarda Artist y Title en tags EXIF visibles (Artist/tag 315, UserComment/tag 37510)
    y almacena el JSON completo en ImageDescription (tag 270).
    
    Args:
        image_path: Ruta a la imagen
        marker: Objeto MuseeImageMarker con los datos
        
    Returns:
        True si la marca fue embebida, False en caso contrario
    """
    try:
        if not image_path.exists():
            logger.warning(f"Imagen no existe: {image_path}")
            return False
        
        # Abrir imagen
        img = Image.open(image_path)
        marker_json = marker.to_json()
        
        # JPEG - usar EXIF
        if image_path.suffix.lower() in ['.jpg', '.jpeg']:
            try:
                from PIL.Image import Exif
                
                exif = img.getexif()
                
                # Para carpetas y canciones: mostrar todo en tag 270 (Title/ImageDescription)
                # Formato: "Artist - Album - Type" o "Artist - Title - Album"
                if marker.image_type == "folder" or marker.image_type == "album":
                    # CARPETA/ALBUM: "Artist - Album"
                    display_name = ""
                    if marker.artist:
                        display_name = marker.artist
                        if marker.album:
                            display_name = f"{marker.artist} - {marker.album}"
                    
                    if display_name:
                        exif[270] = display_name
                        
                elif marker.image_type == "song":
                    # CANCIÓN: "Artist - Title - Album"
                    display_name = ""
                    if marker.artist:
                        display_name = marker.artist
                        if marker.title:
                            display_name = f"{marker.artist} - {marker.title}"
                            if marker.album:
                                display_name = f"{marker.artist} - {marker.title} - {marker.album}"
                    elif marker.title:
                        display_name = marker.title
                    
                    if display_name:
                        exif[270] = display_name
                
                # Guardar JSON en tag 37500 (MakerNote) - NO visible en Windows Explorer
                # Esto permite almacenar el JSON sin que aparezca en el título
                exif[37500] = marker_json
                
                # Limpiar otros tags que pueden causar conflicto
                for tag_id in [315, 40092, 37510]:
                    if tag_id in exif:
                        del exif[tag_id]
                
                # Guardar con EXIF
                img.save(image_path, exif=exif, quality=95)
                logger.debug(f"Metadata embebida en JPEG: {image_path.name} (Type: {marker.image_type})")
                return True
                
            except Exception as e:
                logger.warning(f"Error al embeber metadata EXIF en JPEG: {e}")
                # Fallback: guardar sin metadata pero sin fallar
                img.save(image_path, quality=95)
                return False
        
        # PNG - usar PNG metadata
        elif image_path.suffix.lower() == '.png':
            try:
                # PNG permite metadata chunks personalizados
                from PIL import PngImagePlugin
                
                # Crear PngInfo para almacenar metadata
                pnginfo = PngImagePlugin.PngInfo()
                
                # Guardar metadata legible y JSON completo
                if marker.artist:
                    pnginfo.add_text("Artist", marker.artist)
                if marker.album:
                    pnginfo.add_text("Album", marker.album)
                if marker.title and marker.image_type == "song":
                    pnginfo.add_text("Title", marker.title)
                
                # Guardar JSON completo
                pnginfo.add_text(MUSEE_MARKER_KEY, marker_json)
                
                # Guardar PNG con metadata
                img.save(image_path, pnginfo=pnginfo)
                logger.debug(f"Metadata embebida en PNG: {image_path.name}")
                return True
                
            except Exception as e:
                logger.warning(f"Error al embeber metadata PNG: {e}")
                img.save(image_path)
                return False
        
        else:
            logger.warning(f"Formato de imagen no soportado para metadata: {image_path.suffix}")
            return False
            
    except Exception as e:
        logger.error(f"Error al embeber metadata: {e}")
        return False


def detect_musee_image(image_path: Path) -> Tuple[bool, Optional[MuseeImageMarker]]:
    """
    Detecta si una imagen fue descargada por MUSEE buscando la marca.
    
    Busca la marca completa en:
    - Para JPEG: EXIF tag 270 (ImageDescription) con JSON completo prefijado con "MUSEE:"
    - Para PNG: Chunk personalizado con key MUSEE_MARKER_KEY
    
    Si no encuentra la marca completa pero sí encuentra metadata visible (Artist/Title),
    registra que parece ser una imagen marcada.
    
    Args:
        image_path: Ruta a la imagen
        
    Returns:
        Tupla (is_musee_image, marker_data)
        - is_musee_image: True si la imagen tiene marca MUSEE
        - marker_data: Objeto MuseeImageMarker si fue detectado, None en caso contrario
    """
    try:
        if not image_path.exists():
            logger.debug(f"Imagen no existe: {image_path}")
            return False, None
        
        img = Image.open(image_path)
        
        # JPEG - buscar en EXIF
        if image_path.suffix.lower() in ['.jpg', '.jpeg']:
            try:
                exif = img.getexif()
                
                # Buscar JSON completo en MakerNote (tag 37500) - lugar oculto donde guardamos JSON
                if 37500 in exif:
                    marker_json = exif[37500]
                    if isinstance(marker_json, str):
                        # Verificar que es JSON con nuestra marca
                        if MUSEE_MARKER_KEY in marker_json or "version" in marker_json:
                            marker = MuseeImageMarker.from_json(marker_json)
                            logger.debug(f"Marca MUSEE detectada en JPEG (tag 37500): {image_path.name}")
                            return True, marker
                
                # Fallback: buscar en ImageDescription (tag 270) para compatibilidad con versiones antiguas
                if 270 in exif:
                    marker_text = exif[270]
                    if isinstance(marker_text, str):
                        # Remover prefijo "MUSEE:" si existe (de versión anterior)
                        if marker_text.startswith("MUSEE:"):
                            marker_json = marker_text[6:]  # Remover "MUSEE:"
                        else:
                            marker_json = marker_text
                        
                        # Verificar que es JSON con nuestra marca
                        if MUSEE_MARKER_KEY in marker_json or "version" in marker_json:
                            try:
                                marker = MuseeImageMarker.from_json(marker_json)
                                logger.debug(f"Marca MUSEE detectada en JPEG (tag 270 legacy): {image_path.name}")
                                return True, marker
                            except:
                                pass
                
                # Fallback: si encuentra Artist (tag 315) pero sin marca JSON
                if 315 in exif and isinstance(exif[315], str):
                    logger.debug(f"Se encontró Artist en EXIF pero sin marca JSON completa: {image_path.name}")
                    
            except Exception as e:
                logger.debug(f"Error al leer EXIF: {e}")
        
        # PNG - buscar en metadata
        elif image_path.suffix.lower() == '.png':
            try:
                # PIL almacena el texto en img.info dict con clave que viene de PNG
                if hasattr(img, 'info'):
                    # Buscar la clave MUSEE_MARKER_KEY en info
                    if MUSEE_MARKER_KEY in img.info:
                        marker_json = img.info[MUSEE_MARKER_KEY]
                        marker = MuseeImageMarker.from_json(marker_json)
                        logger.debug(f"Marca MUSEE detectada en PNG: {image_path.name}")
                        return True, marker
                    # También buscar por cualquier clave que contenga nuestra marca
                    for key, value in img.info.items():
                        if isinstance(value, str) and '"source"' in value and '"version"' in value:
                            try:
                                marker = MuseeImageMarker.from_json(value)
                                logger.debug(f"Marca MUSEE detectada en PNG (key={key}): {image_path.name}")
                                return True, marker
                            except:
                                pass
                    
                    # Fallback: buscar metadata legible en PNG
                    if 'Artist' in img.info or 'Album' in img.info:
                        logger.debug(f"Se encontró metadata de Artist/Album en PNG pero sin marca JSON completa: {image_path.name}")
                    
            except Exception as e:
                logger.debug(f"Error al leer PNG metadata: {e}")
        
        return False, None
        
    except Exception as e:
        logger.debug(f"Error al detectar marca MUSEE: {e}")
        return False, None


def remove_image_marker(image_path: Path) -> bool:
    """
    Elimina la marca invisible de una imagen MUSEE.
    
    Nota: Simplemente abre, re-guarda sin marca.
    
    Args:
        image_path: Ruta a la imagen
        
    Returns:
        True si la marca fue eliminada, False en caso contrario
    """
    try:
        if not image_path.exists():
            logger.warning(f"Imagen no existe: {image_path}")
            return False
        
        # Detectar si tiene marca primero
        has_mark, marker = detect_musee_image(image_path)
        if not has_mark:
            logger.debug(f"Imagen no tiene marca MUSEE: {image_path.name}")
            return False
        
        img = Image.open(image_path)
        
        # JPEG - limpiar EXIF tag 270
        if image_path.suffix.lower() in ['.jpg', '.jpeg']:
            try:
                from PIL.Image import Exif
                exif = img.getexif()
                if 270 in exif:
                    del exif[270]
                img.save(image_path, exif=exif, quality=95)
                logger.debug(f"Marca eliminada de JPEG: {image_path.name}")
                return True
            except Exception as e:
                logger.warning(f"Error al eliminar marca JPEG: {e}")
                return False
        
        # PNG - re-guardar sin metadata
        elif image_path.suffix.lower() == '.png':
            try:
                # Crear nueva imagen sin metadata
                new_img = Image.new(img.mode, img.size)
                new_img.putdata(img.getdata())
                new_img.save(image_path)
                logger.debug(f"Marca eliminada de PNG: {image_path.name}")
                return True
            except Exception as e:
                logger.warning(f"Error al eliminar marca PNG: {e}")
                return False
        
        return False
        
    except Exception as e:
        logger.error(f"Error al eliminar marca invisible: {e}")
        return False
