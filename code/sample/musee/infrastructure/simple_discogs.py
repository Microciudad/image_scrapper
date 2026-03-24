"""Simple Discogs API client using requests."""

import logging
import time
import requests
from typing import List, Optional

from ..domain.models import DiscogsRelease

logger = logging.getLogger(__name__)


class SimpleDiscogsRepository:
    """Repositorio simple para interactuar con la API REST de Discogs."""
    
    def __init__(self, token: str, rate_limit_delay: float = 1.0):
        if not token or not token.strip():
            raise ValueError("Token de Discogs no válido o vacío")
        
        self.token = token
        self.session = requests.Session()
        self.session.headers.update({
            'User-Agent': 'musee/1.0',
            'Authorization': f'Discogs token={token}'
        })
        self.base_url = 'https://api.discogs.com'
        self._rate_limit_delay = rate_limit_delay
        self._token_valid = None  # Cache para validación de token
        
        # Validar token en inicialización
        if not self._validate_token():
            raise ValueError("Token de Discogs inválido o expirado")
    
    def _validate_token(self) -> bool:
        """Valida que el token de Discogs sea válido."""
        if self._token_valid is not None:
            return self._token_valid
        
        try:
            # Hacer una request simple para validar el token
            response = self.session.get(
                f'{self.base_url}/users/me',
                timeout=10
            )
            
            if response.status_code == 401:
                logger.error("Token de Discogs inválido o expirado (401 Unauthorized)")
                self._token_valid = False
                return False
            elif response.status_code == 403:
                logger.error("Token de Discogs sin permisos suficientes (403 Forbidden)")
                self._token_valid = False
                return False
            elif response.status_code == 200:
                logger.info("Token de Discogs validado correctamente")
                self._token_valid = True
                return True
            else:
                logger.warning(f"Error inesperado al validar token: {response.status_code}")
                self._token_valid = True  # Asumir que es válido si no es error de autenticación
                return True
        except Exception as e:
            logger.error(f"Error validando token: {e}")
            raise ValueError(f"No se pudo validar el token de Discogs: {e}")
    
    def search_album(self, artist: str, album: str, year: Optional[int] = None) -> List[DiscogsRelease]:
        """Busca un álbum en Discogs usando la API REST.
        
        Args:
            artist: Nombre del artista
            album: Nombre del álbum
            year: Año del álbum (opcional, ayuda a filtrar resultados exactos)
        """
        try:
            # Limpiar nombres para búsqueda
            clean_artist = artist.replace("-", " ").strip()
            clean_album = album.replace("-", " ").strip()
            full_name = f"{clean_artist} {clean_album}".strip()
            
            # Variantes de "Various" que pueden encontrarse
            various_variants = ['various', 'varios', 'variados', 'va', 'v/a', 'v.a', 'v.a.']
            is_various = clean_artist.lower() in various_variants
            
            # Intentar múltiples estrategias de búsqueda (OPTIMIZADO: solo las más efectivas)
            search_strategies = []
            
            # Si el artista es "Various" o sus variantes, usar estrategias específicas
            if is_various:
                # Para Various: solo 2 estrategias - simple + literal
                search_strategies = [
                    f'{clean_artist} - {clean_album}',      # Búsqueda simple (primero - mejor precisión)
                    f'"{clean_album}"',                     # Literal del nombre del álbum (fallback)
                ]
            else:
                # Para otros artistas: solo 2 estrategias - específica + general
                search_strategies = [
                    f'"{clean_artist}" "{clean_album}"',    # Específica
                    f'{full_name}',                         # General
                ]
            
            for i, query in enumerate(search_strategies):
                logger.debug(f"Estrategia {i+1}: {query}")
                
                params = {
                    'q': query,
                    'type': 'release',
                    'per_page': 5  # Reducido de 20 a 5 - es suficiente después del filtrado
                }
                
                response = self.session.get(
                    f'{self.base_url}/database/search',
                    params=params,
                    timeout=30
                )
                
                self._rate_limit()
                
                # Detectar errores de autenticación
                if response.status_code == 401:
                    logger.error("Token de Discogs inválido o expirado")
                    raise ValueError("Token de Discogs inválido o expirado. Por favor, verifica tu token.")
                elif response.status_code == 403:
                    logger.error("Token de Discogs sin permisos suficientes")
                    raise ValueError("Token de Discogs sin permisos suficientes.")
                
                if response.status_code != 200:
                    logger.debug(f"Error en estrategia {i+1}: {response.status_code}")
                    continue
                
                data = response.json()
                results = data.get('results', [])
                
                logger.debug(f"Estrategia {i+1} devolvió {len(results)} resultados")
                
                if results:  # Si encontramos resultados, procesarlos
                    releases = []
                    for j, result in enumerate(results):
                        try:
                            release = self._convert_to_domain_model(result)
                            if release:
                                releases.append(release)
                                logger.debug(f"Release válida: {release.title} - {release.artist}")
                        except Exception as e:
                            logger.warning(f"Error procesando resultado {j+1}: {e}")
                    
                    # Filtrar releases si es necesario (ej: preferir "Various" si el artista es Various/Varios)
                    releases = self._filter_releases_by_artist(releases, clean_artist, is_various)
                    logger.debug(f"Después de filtro artista: {len(releases)} releases")
                    
                    # Filtrar por año PRIMERO si se proporcionó (mejor precisión)
                    if year and releases:
                        releases = self._filter_releases_by_year(releases, year)
                        logger.debug(f"Después de filtro año ({year}): {len(releases)} releases")
                    
                    # Filtrar por formato ANTES de similitud (CD > LP > Cassette tiene prioridad)
                    if releases:
                        releases = self._filter_releases_by_format(releases)
                        logger.debug(f"Después de filtro formato: {len(releases)} releases")
                    
                    # Filtrar por similitud de título como último desempate
                    releases = self._filter_releases_by_title_similarity(releases, clean_album)
                    logger.debug(f"Después de filtro similitud: {len(releases)} releases")
                    
                    if releases:  # Si hay releases válidas, retornar
                        logger.debug(f"Retornando {len(releases)} releases válidas de estrategia {i+1}")
                        return releases
            
            logger.debug("Ninguna estrategia de búsqueda devolvió resultados")
            return []
            
        except Exception as e:
            logger.error(f"Error buscando álbum en Discogs: {e}")
            return []
    
    def search_single(self, artist: str, title: str) -> List[DiscogsRelease]:
        """Busca una canción en Discogs, considerando que puede estar en un álbum o como single.
        
        Intenta múltiples estrategias de búsqueda:
        1. Búsqueda específica: artist:"X" title:"Y" (sin restricción de formato)
        2. Búsqueda laxa: "X" "Y" (sin especificar campos)
        3. Búsqueda de artista + título juntos
        
        Esto permite encontrar canciones que:
        - Son singles independientes
        - Son parte de un álbum
        - Están mal catalogadas o con variaciones de nombre
        """
        try:
            clean_artist = artist.replace("-", " ").strip()
            clean_title = title.replace("-", " ").strip()
            full_name = f"{clean_artist} {clean_title}".strip()
            
            # Estrategias de búsqueda progresivas (sin restricción de formato)
            search_strategies = [
                # 1. Búsqueda específica: artist y título exactos (mejor precisión)
                f'artist:"{clean_artist}" title:"{clean_title}"',
                # 2. Búsqueda con comillas (literal): ambos términos juntos
                f'"{clean_artist}" "{clean_title}"',
                # 3. Búsqueda laxa: todos los términos (menos restrictiva)
                f'{full_name}',
            ]
            
            for i, query in enumerate(search_strategies, 1):
                logger.debug(f"🔍 ESTRATEGIA {i}/3 | {query}")
                
                params = {
                    'q': query,
                    'type': 'release',  # Solo releases (no masters)
                    'per_page': 10  # Aumentado a 10 para más opciones
                }
                
                response = self.session.get(
                    f'{self.base_url}/database/search',
                    params=params,
                    timeout=30
                )
                
                self._rate_limit()
                
                if response.status_code != 200:
                    logger.debug(f"⚠️  Estrategia {i} error: {response.status_code}")
                    continue
                
                data = response.json()
                results = data.get('results', [])
                
                logger.debug(f"📊 RESULTS | {len(results)} opciones encontradas")
                
                if results:
                    releases = []
                    for idx, result in enumerate(results, 1):
                        try:
                            release = self._convert_to_domain_model(result)
                            if release:
                                image_status = "✓" if release.image_url else "✗"
                                logger.debug(f"  [{idx}] {image_status} {release.title} ({release.year}) - {release.artist}")
                                releases.append(release)
                        except Exception as e:
                            logger.warning(f"⚠️  Error procesando resultado: {e}")
                    
                    # Si encontramos resultados válidos, retornarlos
                    if releases:
                        logger.debug(f"✅ Usando resultados de estrategia {i}")
                        return releases
            
            logger.debug("❌ Ninguna estrategia devolvió resultados")
            return []
            
        except Exception as e:
            logger.error(f"❌ SEARCH ERROR | {e}")
            return []
    
    def _convert_to_domain_model(self, discogs_result: dict) -> Optional[DiscogsRelease]:
        """Convierte un resultado de la API REST a modelo del dominio."""
        try:
            # Obtener imagen de mejor calidad
            image_url = None
            
            # Prioridad 1: cover_image (imagen completa)
            if 'cover_image' in discogs_result and discogs_result['cover_image']:
                image_url = discogs_result['cover_image']
                logger.debug(f"Usando cover_image: {image_url}")
            # Prioridad 2: thumb (pero intentaremos mejorarlo)
            elif 'thumb' in discogs_result and discogs_result['thumb']:
                thumb_url = discogs_result['thumb']
                # Convertir thumbnail a imagen de mayor resolución
                image_url = self._get_high_res_image_url(thumb_url)
                logger.debug(f"Convirtiendo thumb a alta resolución: {image_url}")
            
            # Obtener artistas
            artist = 'Unknown Artist'
            if 'artist' in discogs_result:
                artist = discogs_result['artist']
            elif 'artists' in discogs_result and discogs_result['artists']:
                artist = ', '.join(discogs_result['artists'])
            
            # Obtener el título
            title = discogs_result.get('title', 'Unknown Title')
            
            return DiscogsRelease(
                id=discogs_result.get('id', 0),
                title=title,
                artist=artist,
                year=discogs_result.get('year'),
                image_url=image_url,
                type=discogs_result.get('type', 'release')
            )
            
        except Exception as e:
            logger.error(f"Error convirtiendo resultado de Discogs: {e}")
            return None
    
    def _filter_releases_by_artist(self, releases: List[DiscogsRelease], expected_artist: str, is_various: bool = False) -> List[DiscogsRelease]:
        """Filtra releases para preferir aquellos que coincidan mejor con el artista esperado."""
        # Variantes de "Various" que pueden encontrarse en los resultados de Discogs
        various_variants = ['various', 'varios', 'variados', 'va', 'v/a', 'v.a', 'v.a.']
        
        # Si el artista es "Various" o sus variantes, filtrar resultados que también contengan variantes de "Various"
        if is_various:
            filtered = []
            for release in releases:
                title_lower = release.title.lower()
                # Preferir releases que contengan variantes de "Various" en el título
                for variant in various_variants:
                    if variant in title_lower:
                        filtered.append(release)
                        break
            
            # Si encontramos releases de "Various", usarlos; si no, usar los originales
            if filtered:
                logger.debug(f"Filtrados {len(filtered)} releases de Various de un total de {len(releases)}")
                return filtered
        
        return releases
    
    def _filter_releases_by_title_similarity(self, releases: List[DiscogsRelease], expected_album: str) -> List[DiscogsRelease]:
        """Filtra releases priorizando aquellos cuyo título sea similar al álbum esperado."""
        import difflib
        
        # Limpiar el título esperado
        expected_clean = expected_album.lower().strip()
        
        # Calcular similitud para cada release
        scored_releases = []
        for release in releases:
            title_clean = release.title.lower().strip()
            
            # Calcular ratio de similitud (0.0 a 1.0)
            ratio = difflib.SequenceMatcher(None, expected_clean, title_clean).ratio()
            scored_releases.append((ratio, release))
        
        # Ordenar por similitud descendente
        scored_releases.sort(key=lambda x: x[0], reverse=True)
        
        # Si el mejor match tiene similitud razonable (>0.4), devolver solo releases con esa similitud
        # Esto permite matchear "Master Mix 2" con "Master Mix Vol. 2" (~0.4-0.5 ratio)
        if scored_releases and scored_releases[0][0] >= 0.4:
            good_similarity = [r for score, r in scored_releases if score >= 0.4]
            logger.debug(f"Filtrados {len(good_similarity)} releases con similitud de título (>0.4)")
            return good_similarity
        
        # Si no hay alta similitud, devolver todos ordenados por similitud
        logger.debug(f"No hay releases con alta similitud de título, devolviendo todos ordenados")
        return [r for _, r in scored_releases]
    
    def _filter_releases_by_year(self, releases: List[DiscogsRelease], expected_year: int) -> List[DiscogsRelease]:
        """Filtra releases para preferir aquellos que coincidan con el año esperado."""
        # Helper para convertir año a int
        def get_year_int(year):
            if year is None:
                return None
            if isinstance(year, int):
                return year
            try:
                return int(year)
            except (ValueError, TypeError):
                return None
        
        # Buscar releases exactos del año esperado
        exact_year_matches = [r for r in releases if get_year_int(r.year) == expected_year]
        
        if exact_year_matches:
            logger.debug(f"Encontrados {len(exact_year_matches)} releases del año {expected_year}")
            # Retornar solo los que coinciden exactamente con el año
            return exact_year_matches
        
        # Si no hay coincidencias exactas, usar releases cercanos al año (±2 años)
        year_tolerance = 2
        close_year_matches = []
        for r in releases:
            r_year = get_year_int(r.year)
            if r_year and abs(r_year - expected_year) <= year_tolerance:
                close_year_matches.append(r)
        
        if close_year_matches:
            logger.debug(f"No hay releases exactos del año {expected_year}, usando releases cercanos (±{year_tolerance} años)")
            return close_year_matches
        
        # Si no hay coincidencias cercanas, retornar todos
        logger.debug(f"No hay releases cercanos al año {expected_year}, usando todos los releases")
        return releases
    
    def _filter_releases_by_format(self, releases: List[DiscogsRelease]) -> List[DiscogsRelease]:
        """Filtra releases por formato preferido como desempate (CD > Vinyl/LP > Cassette).
        
        Prioridad:
        1. CD oficial (100 puntos)
        2. CDr/CD-R (90 puntos)
        3. Vinyl/LP (50 puntos)
        4. Cassette (10 puntos) - solo como fallback
        5. Otros (1 punto)
        
        Se retorna solo el grupo con mayor prioridad.
        """
        def get_format_priority(format_list):
            """Retorna puntuación de prioridad para un formato (mayor = mejor)"""
            if not format_list:
                return -1
            
            format_str = ' '.join(format_list).upper()
            
            # CD oficial (mayor prioridad) - NO incluir CDr
            if 'CD' in format_str and 'CDR' not in format_str:
                return 100
            
            # CDr/CD-R (también CD, pero menor que oficial)
            if 'CDR' in format_str or 'CD-R' in format_str:
                return 90
            
            # Vinyl/LP (segunda prioridad)
            if 'VINYL' in format_str or 'LP' in format_str:
                return 50
            
            # Cassette (baja prioridad, solo si no hay nada mejor)
            if 'CASSETTE' in format_str or 'TAPE' in format_str:
                return 10
            
            # Otros
            return 1
        
        if not releases:
            return releases
        
        # Ordenar por prioridad de formato
        releases_with_priority = [(r, get_format_priority(r.type)) for r in releases]
        releases_with_priority.sort(key=lambda x: x[1], reverse=True)
        
        # Retornar solo el grupo con mayor prioridad
        if releases_with_priority:
            best_priority = releases_with_priority[0][1]
            best_format_releases = [r for r, p in releases_with_priority if p == best_priority]
            if best_format_releases:
                logger.debug(f"Filtradas {len(releases)} releases por formato, {len(best_format_releases)} con mayor prioridad")
                return best_format_releases
        
        return releases
    
    def _get_high_res_image_url(self, thumb_url: str) -> str:
        """Convierte una URL de thumbnail a una de mayor resolución."""
        try:
            # Los thumbnails de Discogs tienen patrones como:
            # https://i.discogs.com/...../rs:fit/g:sm/q:40/h:150/w:150/....jpeg
            # Podemos modificar los parámetros para obtener mayor resolución
            
            if 'i.discogs.com' in thumb_url and '/rs:fit/' in thumb_url:
                # Reemplazar parámetros de resolución por otros más altos
                high_res_url = thumb_url
                # Cambiar h:150/w:150 por h:600/w:600 o h:500/w:500
                high_res_url = high_res_url.replace('/h:150/w:150/', '/h:600/w:600/')
                high_res_url = high_res_url.replace('/h:300/w:300/', '/h:600/w:600/')
                # Mejorar calidad de 40 a 90
                high_res_url = high_res_url.replace('/q:40/', '/q:90/')
                high_res_url = high_res_url.replace('/q:50/', '/q:90/')
                
                logger.debug(f"URL mejorada: {thumb_url} -> {high_res_url}")
                return high_res_url
            
            return thumb_url
            
        except Exception as e:
            logger.warning(f"Error mejorando URL de imagen: {e}")
            return thumb_url
    
    def _rate_limit(self):
        """Aplica rate limiting para respetar la API de Discogs."""
        time.sleep(self._rate_limit_delay)
