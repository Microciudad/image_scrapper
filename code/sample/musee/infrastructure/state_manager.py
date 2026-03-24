"""
Gestor de estado persistente para pausar/reanudar procesamiento.

Guarda el estado de búsquedas en bloqueos de Google para poder continuar
en otro momento cuando se resuelva el rate limit.
"""

import json
import logging
import re
from pathlib import Path
from dataclasses import dataclass, asdict, field
from typing import Optional, Dict, Any, List
from datetime import datetime

logger = logging.getLogger(__name__)


def normalize_path_for_comparison(path_str: str) -> str:
    r"""
    Normaliza paths para comparación, soportando múltiples formatos:
    
    Soporta:
    - Windows: C:\path\to\folder, C:/path/to/folder
    - WSL: /mnt/c/path/to/folder, /c/path/to/folder, G:/path, /g/path
    - Cygwin: /cygdrive/c/path/to/folder
    - Bash/MSYS: /c/path/to/folder
    - UNC paths: \\server\share, //server/share
    
    Retorna una representación normalizada (lowercase, forward slashes, sin drive letter):
    /c/path/to/folder (para cualquier formato que represente C:\path\to\folder)
    """
    if not path_str:
        return ""
    
    path_str = str(path_str).strip()
    
    # Replace all backslashes with forward slashes
    path_str = path_str.replace('\\', '/')
    
    # Handle UNC paths: //server/share or \\server\share -> //server/share
    if path_str.startswith('//'):
        return path_str.lower()
    
    # Handle different WSL/Cygwin path formats
    # /mnt/c/path -> /c/path (WSL style)
    path_str = re.sub(r'^/mnt/([a-z])/', r'/\1/', path_str)
    
    # /cygdrive/c/path -> /c/path (Cygwin style)
    path_str = re.sub(r'^/cygdrive/([a-z])/', r'/\1/', path_str)
    
    # Handle Windows drive letters at start: c:/path or C:/path -> /c/path
    if len(path_str) >= 2 and path_str[1] == ':':
        path_str = '/' + path_str[0] + path_str[2:]
    
    # Remove trailing slashes (except root)
    path_str = path_str.rstrip('/')
    if path_str == '':
        path_str = '/'
    
    # Convert to lowercase for case-insensitive comparison
    return path_str.lower()



@dataclass
class ProcessingState:
    """Estado de una búsqueda en progreso."""
    artist: str
    album: Optional[str] = None
    title: Optional[str] = None
    query_type: str = "album"  # 'album' o 'single'
    search_index: int = 0  # Índice de búsqueda (cual query estamos probando)
    status: str = "pending"  # pending, searching, blocked, completed, failed
    error: Optional[str] = None
    timestamp: str = ""  # ISO format
    
    def to_dict(self) -> Dict[str, Any]:
        """Convierte a diccionario."""
        return asdict(self)
    
    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'ProcessingState':
        """Crea desde diccionario."""
        return cls(**data)


@dataclass
class ProcessingRunState:
    """Estado de una ejecución completa de processing (carpetas/canciones)."""
    path: str  # Path absoluto de la carpeta siendo procesada
    action: str  # 'get_folder_image', 'get_song_image', 'both'
    total_items: int = 0  # Total de items a procesar
    processed_items: int = 0  # Items ya procesados
    last_processed_item: Optional[str] = None  # Nombre del último item procesado
    status: str = "in_progress"  # in_progress, completed, interrupted
    timestamp: str = ""  # ISO format
    interrupted_at: Optional[str] = None  # Timestamp de interrupción
    processed_item_paths: List[str] = field(default_factory=list)  # Track by path instead of index for randomized processing
    
    def to_dict(self) -> Dict[str, Any]:
        """Convierte a diccionario."""
        return asdict(self)
    
    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'ProcessingRunState':
        """Crea desde diccionario."""
        return cls(**data)


class StateManager:
    """Gestiona el estado persistente de búsquedas."""
    
    def __init__(self):
        """Inicializa el gestor de estado."""
        # Guardar en ~/.musee/state.json
        self.state_dir = Path.home() / ".musee"
        self.state_file = self.state_dir / "state.json"
        self.run_state_file = self.state_dir / "run_state.json"
        self.state_dir.mkdir(exist_ok=True)
        
        logger.debug(f"StateManager inicializado: {self.state_file}")
        logger.debug(f"Run state file: {self.run_state_file}")
    
    def save_state(self, state: ProcessingState) -> None:
        """
        Guarda el estado de una búsqueda.
        
        Args:
            state: Estado a guardar
        """
        try:
            # Actualizar timestamp
            state.timestamp = datetime.now().isoformat()
            
            # Leer estado actual
            states = self._load_all_states()
            
            # Crear clave única para esta búsqueda
            key = self._make_key(state.artist, state.album, state.title, state.query_type)
            
            # Actualizar o agregar
            states[key] = state.to_dict()
            
            # Guardar
            with open(self.state_file, 'w') as f:
                json.dump(states, f, indent=2)
            
            logger.debug(f"Estado guardado: {key} -> {state.status}")
        
        except Exception as e:
            logger.error(f"Error guardando estado: {e}")
    
    def load_state(self, artist: str, album: Optional[str] = None, 
                   title: Optional[str] = None, query_type: str = "album") -> Optional[ProcessingState]:
        """
        Carga el estado de una búsqueda.
        
        Args:
            artist: Artista
            album: Álbum (para búsquedas de álbum)
            title: Título (para búsquedas de single)
            query_type: Tipo de búsqueda ('album' o 'single')
        
        Returns:
            ProcessingState si existe, None en caso contrario
        """
        try:
            key = self._make_key(artist, album, title, query_type)
            states = self._load_all_states()
            
            if key in states:
                state_data = states[key]
                state = ProcessingState.from_dict(state_data)
                logger.debug(f"Estado cargado: {key} -> {state.status}")
                return state
            
            return None
        
        except Exception as e:
            logger.error(f"Error cargando estado: {e}")
            return None
    
    def delete_state(self, artist: str, album: Optional[str] = None,
                    title: Optional[str] = None, query_type: str = "album") -> None:
        """
        Elimina el estado de una búsqueda (cuando se completa).
        
        Args:
            artist: Artista
            album: Álbum (para búsquedas de álbum)
            title: Título (para búsquedas de single)
            query_type: Tipo de búsqueda ('album' o 'single')
        """
        try:
            key = self._make_key(artist, album, title, query_type)
            states = self._load_all_states()
            
            if key in states:
                del states[key]
                with open(self.state_file, 'w') as f:
                    json.dump(states, f, indent=2)
                logger.debug(f"Estado eliminado: {key}")
        
        except Exception as e:
            logger.error(f"Error eliminando estado: {e}")
    
    def get_blocked_searches(self) -> list:
        """
        Retorna todas las búsquedas bloqueadas por Google.
        
        Returns:
            Lista de ProcessingState en estado 'blocked'
        """
        try:
            states = self._load_all_states()
            blocked = [
                ProcessingState.from_dict(data)
                for data in states.values()
                if data.get('status') == 'blocked'
            ]
            return blocked
        
        except Exception as e:
            logger.error(f"Error obteniendo búsquedas bloqueadas: {e}")
            return []
    
    def _load_all_states(self) -> Dict[str, Dict[str, Any]]:
        """Carga todos los estados."""
        if not self.state_file.exists():
            return {}
        
        try:
            with open(self.state_file, 'r') as f:
                return json.load(f)
        except Exception as e:
            logger.error(f"Error leyendo archivo de estado: {e}")
            return {}
    
    def _make_key(self, artist: str, album: Optional[str] = None,
                  title: Optional[str] = None, query_type: str = "album") -> str:
        """Crea una clave única para una búsqueda."""
        if query_type == "album" and album:
            return f"album:{artist}:{album}"
        elif query_type == "single" and title:
            return f"single:{artist}:{title}"
        else:
            return f"unknown:{artist}"
    
    # ==================== RUN STATE MANAGEMENT ====================
    # Gestiona el estado de ejecuciones completas (cuando se procesan carpetas/canciones)
    
    def save_run_state(self, run_state: ProcessingRunState) -> None:
        """
        Guarda el estado de una ejecución de procesamiento.
        
        Args:
            run_state: Estado de la ejecución a guardar
        """
        try:
            # Actualizar timestamp
            run_state.timestamp = datetime.now().isoformat()
            
            # Crear estado diccionario
            state_dict = run_state.to_dict()
            
            # Guardar
            with open(self.run_state_file, 'w') as f:
                json.dump(state_dict, f, indent=2)
            
            logger.debug(f"Estado de ejecución guardado: {run_state.path} ({run_state.processed_items}/{run_state.total_items})")
        
        except Exception as e:
            logger.error(f"Error guardando estado de ejecución: {e}")
    
    def load_run_state(self, path: str, action: str) -> Optional[ProcessingRunState]:
        """
        Carga el estado de una ejecución de procesamiento.
        
        Args:
            path: Path de la carpeta siendo procesada (puede venir de WSL, Cygwin, Windows, PowerShell, etc.)
            action: Acción ('get_folder_image', 'get_song_image', 'both')
        
        Returns:
            ProcessingRunState si existe, None en caso contrario
        """
        try:
            if not self.run_state_file.exists():
                return None
            
            with open(self.run_state_file, 'r') as f:
                state_dict = json.load(f)
            
            # Normalize paths for comparison (soporta WSL, Cygwin, Windows, PowerShell, Bash/MSYS)
            saved_path = normalize_path_for_comparison(state_dict.get('path', ''))
            current_path = normalize_path_for_comparison(path)
            
            logger.debug(f"Comparing paths: saved='{saved_path}' vs current='{current_path}'")
            
            # Verificar que es para el mismo path y acción
            if saved_path == current_path and state_dict.get('action') == action:
                state = ProcessingRunState.from_dict(state_dict)
                logger.debug(f"Estado de ejecución cargado: {path} ({state.processed_items}/{state.total_items})")
                return state
            
            return None
        
        except Exception as e:
            logger.error(f"Error cargando estado de ejecución: {e}")
            return None
    
    def delete_run_state(self) -> None:
        """
        Elimina el estado de ejecución (cuando se completa exitosamente).
        """
        try:
            if self.run_state_file.exists():
                self.run_state_file.unlink()
                logger.debug(f"Estado de ejecución eliminado")
        
        except Exception as e:
            logger.error(f"Error eliminando estado de ejecución: {e}")
    
    def get_run_state_file_path(self) -> Path:
        """Retorna la ruta del archivo de estado de ejecución."""
        return self.run_state_file
