"""Musee - CLI para gestión de imágenes de álbumes y canciones musicales."""

__version__ = "0.1.0"
__author__ = "microciudad@gmail.com"

import logging
import os
from pathlib import Path

# Cargar variables de entorno desde .env
from dotenv import load_dotenv
env_file = Path(__file__).parent.parent.parent / '.env'
if env_file.exists():
    load_dotenv(env_file)

def configure_logging(debug: bool = False):
    """Configura el nivel de logging según el flag debug."""
    level = logging.DEBUG if debug else logging.WARNING
    
    # Remover handlers existentes para reconfiguraciones
    for handler in logging.root.handlers[:]:
        logging.root.removeHandler(handler)
    
    logging.basicConfig(
        level=level,
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
        force=True  # Python 3.8+ - force reconfiguration
    )

# Configurar logging por defecto (WARNING)
configure_logging(debug=False)
