"""
Scraper de imágenes desde fuentes públicas con ofuscamiento anti-bloqueo.

Proporciona acceso a imágenes de múltiples fuentes (Discogs, Last.fm, MusicBrainz, etc)
sin API key, ideal para grandes volúmenes de descargas. Implementa rotación de 
User-Agents, delays aleatorios, y técnicas de ofuscamiento para evitar bloqueos.
"""

import logging
import random
import time
import re
import base64
from urllib.parse import urlencode
from dataclasses import dataclass
from typing import Optional, List
from pathlib import Path
import json

import requests
from PIL import Image
from io import BytesIO

logger = logging.getLogger(__name__)

# User-Agents variados para ofuscamiento
USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:121.0) Gecko/20100101 Firefox/121.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.1 Safari/605.1.15",
    "Mozilla/5.0 (X11; Linux x86_64; rv:121.0) Gecko/20100101 Firefox/121.0",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Edge/120.0.0.0",
    "Mozilla/5.0 (iPad; CPU OS 17_2 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.2 Mobile/15E148 Safari/604.1",
]

# Lenguajes variados
ACCEPT_LANGUAGES = [
    "es-ES,es;q=0.9,en;q=0.8",
    "en-US,en;q=0.9,es;q=0.8",
    "fr-FR,fr;q=0.9,en;q=0.8",
    "de-DE,de;q=0.9,en;q=0.8",
]


@dataclass
class ImageData:
    """Datos de imagen descargada."""
    data: bytes
    url: str
    width: Optional[int] = None
    height: Optional[int] = None
    source: str = "web"


@dataclass
class GoogleImageResult:
    """Resultado de búsqueda de Google Images."""
    title: str
    image_url: str
    source_url: str


class GoogleImageScraper:
    """Scraper de imágenes desde múltiples fuentes públicas con protección anti-bloqueo."""
    
    def __init__(self, min_delay: float = 1.0, max_delay: float = 5.0, 
                 timeout: float = 30.0, proxies: Optional[dict] = None, search_strategy: str = 'lastfm'):
        """
        Inicializa el scraper de imágenes.
        
        Args:
            min_delay: Delay mínimo entre requests en segundos
            max_delay: Delay máximo entre requests en segundos
            timeout: Timeout para requests HTTP en segundos
            proxies: Dict de proxies opcionales {'https': 'proxy_url', ...}
            search_strategy: Estrategia de búsqueda: 'lastfm' (solo Last.fm) o 'discogs' (Last.fm + Discogs)
        """
        self.min_delay = min_delay
        self.max_delay = max_delay
        self.timeout = timeout
        self.proxies = proxies or {}
        self.search_strategy = search_strategy
        self.session = requests.Session()
        
        # Gestor de estado para pausar/reanudar en bloqueos
        from .state_manager import StateManager
        self.state_manager = StateManager()
        
        # Configurar para seguir redirects y mantener cookies
        from requests.adapters import HTTPAdapter
        from urllib3.util.retry import Retry
        
        # Reintentos automáticos en caso de error de conexión
        # NO reintentar en 429 (rate limit) porque solo empeora la situación
        retry_strategy = Retry(
            total=1,
            backoff_factor=0.5,
            status_forcelist=[500, 502, 503, 504],  # NO incluir 429
            allowed_methods=["GET", "HEAD"]
        )
        adapter = HTTPAdapter(max_retries=retry_strategy)
        self.session.mount("http://", adapter)
        self.session.mount("https://", adapter)
        
        self._last_request_time = 0
        self._last_search_queries = []  # Track queries for debugging
        self._last_response_html = None  # Track last HTML response for inspection
        self._rate_limit_hits = 0  # Count consecutive rate limit errors for exponential backoff
        self._base_sleep_time = 300  # 5 minutes base sleep time for rate limits
        
        logger.info(f"GoogleImageScraper inicializado: delay={min_delay}-{max_delay}s, timeout={timeout}s, strategy={search_strategy}")
    
    def _get_random_user_agent(self) -> str:
        """Retorna un User-Agent aleatorio."""
        return random.choice(USER_AGENTS)
    
    def _get_random_accept_language(self) -> str:
        """Retorna un Accept-Language aleatorio."""
        return random.choice(ACCEPT_LANGUAGES)
    
    def _rotate_session(self):
        """Crea una nueva sesión con cookies/config limpias para evadir bloqueos."""
        logger.debug("🔄 Rotando sesión para evadir detección de Google...")
        self.session = requests.Session()
        
        # Reconfigurar adaptadores con reintentos
        from requests.adapters import HTTPAdapter
        from urllib3.util.retry import Retry
        
        retry_strategy = Retry(
            total=1,
            backoff_factor=0.5,
            status_forcelist=[500, 502, 503, 504],
            allowed_methods=["GET", "HEAD"]
        )
        adapter = HTTPAdapter(max_retries=retry_strategy)
        self.session.mount("http://", adapter)
        self.session.mount("https://", adapter)
    
    def _get_headers(self, referrer: Optional[str] = None) -> dict:
        """Construye headers ofuscados con variedad de browser fingerprinting."""
        # Note: Removed Sec-Fetch-* headers as they trigger Google bot detection
        # These headers are only needed for actual browser navigation, not API requests
        # Their presence causes Google to return JavaScript-rendered content instead of direct URLs
        headers = {
            "User-Agent": self._get_random_user_agent(),
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
            "Accept-Language": self._get_random_accept_language(),
            "Accept-Encoding": "gzip, deflate",  # IMPORTANT: Do NOT include 'br' (Brotli) - it returns corrupted/binary data!
            "Connection": "keep-alive",
            "Upgrade-Insecure-Requests": "1",
            "Cache-Control": "max-age=0",
            "Pragma": "no-cache",
            "DNT": "1",
            # Add realistic referrer to look like user navigation
            "Referer": referrer or random.choice([
                "https://www.google.com/",
                "https://www.google.es/",
                "https://www.duckduckgo.com/",
                "https://www.startpage.com/",
            ]),
        }
        return headers
    
    def _apply_delay(self):
        """Aplica delay aleatorio para evitar rate limiting."""
        elapsed = time.time() - self._last_request_time
        delay = random.uniform(self.min_delay, self.max_delay)
        
        if elapsed < delay:
            sleep_time = delay - elapsed
            logger.debug(f"Esperando {sleep_time:.2f}s antes del próximo request...")
            time.sleep(sleep_time)
        
        self._last_request_time = time.time()
    
    def _handle_rate_limit(self, query: str = ""):
        """
        Maneja un error de rate limit con backoff exponencial.
        
        - Incrementa el contador de hits
        - Calcula sleep time con backoff exponencial
        - Duerme y registra el evento
        - Continúa después del sleep
        
        Args:
            query: Término de búsqueda para logging
        """
        self._rate_limit_hits += 1
        
        # Exponential backoff: 5 min, 10 min, 20 min, etc.
        sleep_time = self._base_sleep_time * (2 ** (self._rate_limit_hits - 1))
        
        minutes = sleep_time / 60
        logger.warning(f"🚫 GOOGLE RATE LIMIT DETECTED (hit #{self._rate_limit_hits})")
        logger.warning(f"⏸️  Sleeping for {minutes:.1f} minutes ({int(sleep_time)} seconds)...")
        if query:
            logger.warning(f"   Will retry search: {query}")
        logger.info(f"   [Pause started at {time.strftime('%H:%M:%S')}]")
        
        # Dormir en intervalos para permitir logging de progreso
        start_time = time.time()
        while time.time() - start_time < sleep_time:
            remaining = sleep_time - (time.time() - start_time)
            if remaining > 0:
                # Log cada minuto
                if int(remaining) % 60 == 0 or remaining < 5:
                    logger.info(f"   ⏳ Sleeping ({remaining:.0f} seconds remaining to complete {minutes:.0f} minutes)")
                time.sleep(1)
        
        logger.info(f"✅ Rate limit sleep finished - resuming search at {time.strftime('%H:%M:%S')}")
        
        # Rotate session to clear cookies and get a fresh client identity
        self._rotate_session()
        
        # Add human-like delay before retrying to look more natural
        extra_delay = random.uniform(3, 8)
        logger.debug(f"Adding human-like delay before retry: {extra_delay:.1f}s")
        time.sleep(extra_delay)
    
    def get_last_search_queries(self) -> List[dict]:
        """Get the search queries from the last search (for debugging).
        
        Returns:
            List of dicts with 'search_term', 'full_url', 'status', and 'images_found'
        """
        return self._last_search_queries
    
    def get_last_response_html(self) -> Optional[str]:
        """Get the last HTML response from Google (for inspection).
        
        Returns:
            HTML string or None if no response yet
        """
        return self._last_response_html
    
    def _search_lastfm_images(self, query: str, max_results: int = 5) -> List[str]:
        """
        Busca imágenes en Last.fm API.
        
        Args:
            query: Término de búsqueda (idealmente "artist album" o "artist title")
            max_results: Número máximo de resultados
            
        Returns:
            Lista de URLs de imágenes encontradas en Last.fm
        """
        # No aplicar delay para Last.fm (no tiene rate limits como Google)
        
        image_urls = []
        
        try:
            logger.debug(f"Buscando en Last.fm: {query}")
            clean_query = query.replace('"', '').strip()
            
            # Use Last.fm official REST API (not the web UI API)
            url = "http://ws.audioscrobbler.com/2.0/"
            
            # Obtener API key desde variable de entorno (cargada en __init__.py)
            import os
            lastfm_api_key = os.getenv('LASTFM_API_KEY', '4c1fa5f6ec908a56c6ce1fd0e5a8c6f6')
            
            # Parse query to extract artist and album
            # Query format is typically: "Artist Album" or "Artist - Album"
            parts = clean_query.split(' - ') if ' - ' in clean_query else clean_query.rsplit(' ', 1) if len(clean_query.split()) > 1 else (clean_query, '')
            artist = parts[0].strip() if parts else ''
            album = parts[1].strip() if len(parts) > 1 else ''
            
            # If no album part found, try using the full query as album with empty artist
            if not album:
                album = clean_query
            
            params = {
                "method": "album.search",
                "album": album,
                "artist": artist if artist else None,
                "api_key": lastfm_api_key,
                "format": "json"
            }
            # Remove None values from params
            params = {k: v for k, v in params.items() if v is not None}
            
            try:
                response = self.session.get(
                    url,
                    params=params,
                    headers=self._get_headers(),
                    proxies=self.proxies,
                    timeout=self.timeout
                )
                response.raise_for_status()
                
                data = response.json()
                # Last.fm REST API returns results in "results" with "albummatches"
                # Each album match has an "image" array with different sizes
                if data.get("results", {}).get("albummatches"):
                    for match in data["results"]["albummatches"].get("album", []):
                        if match.get("image"):
                            # image is an array of image dicts with size and #text (URL)
                            for img in match["image"]:
                                # Get extralarge image, fallback to large
                                if img.get("#text"):
                                    size = img.get("size", "")
                                    if size in ("extralarge", "large", "medium"):
                                        image_urls.append(img["#text"])
                                        if len(image_urls) >= max_results:
                                            break
                        if len(image_urls) >= max_results:
                            break
                
                if image_urls:
                    logger.debug(f"Encontradas {len(image_urls)} URLs en Last.fm")
                    return image_urls[:max_results]
                    
            except Exception as e:
                logger.debug(f"Error en búsqueda de Last.fm: {e}")
        
        except Exception as e:
            logger.debug(f"Error buscando en Last.fm: {e}")
        
        logger.debug(f"No se encontraron imágenes en Last.fm para: {query}")
        return []
    
    def _filter_image_urls(self, url_list):
        """
        Filter image URLs to remove Google resources, logos, and suspicious URLs.
        
        Args:
            url_list: List of URLs to filter
            
        Returns:
            Filtered list of valid image URLs
        """
        google_exclude_patterns = ['google.com', 'gstatic.com/gb', 'logo', 'icon', 'ads']
        suspicious_patterns = ['1x1', 'pixel', 'spacer']
        
        filtered = []
        for url_candidate in set(url_list):
            # Skip Google resources
            if any(pattern in url_candidate.lower() for pattern in google_exclude_patterns):
                logger.debug(f"Skipping Google URL: {url_candidate[:80]}")
                continue
            
            # Skip suspicious URLs
            if any(x in url_candidate.lower() for x in suspicious_patterns):
                logger.debug(f"Skipping suspicious URL: {url_candidate[:80]}")
                continue
            
            filtered.append(url_candidate)
        
        return filtered
    
    def _detect_response_type(self, response_text):
        """
        Detect the type of response structure Google sent.
        Returns a tuple: (response_type, characteristics_dict)
        
        Types:
        - 'TYPE_1_BASE64': Has embedded base64 images via _setImagesSrc (best)
        - 'TYPE_2_DEFERRED': Has data-deferred lazy-loading placeholders (needs fallback)
        - 'TYPE_3_DIRECT_URLS': Has direct JPG/PNG URLs in HTML
        - 'TYPE_4_GSTATIC': Has encrypted-tbn0.gstatic URLs
        - 'TYPE_5_JSDATA': Has encoded jsdata attributes (requires JS execution)
        - 'TYPE_UNKNOWN': Unrecognized structure
        """
        # Try to extract actual URLs to determine if direct URLs exist
        # Use multiple patterns to catch URLs in different contexts
        jpg_urls = re.findall(r'https?://[^\s<>"]+\.(jpg|jpeg|jpe)[^\s<>"]*', response_text, re.IGNORECASE)
        png_urls = re.findall(r'https?://[^\s<>"]+\.png[^\s<>"]*', response_text, re.IGNORECASE)
        webp_urls = re.findall(r'https?://[^\s<>"]+\.webp[^\s<>"]*', response_text, re.IGNORECASE)
        gif_urls = re.findall(r'https?://[^\s<>"]+\.gif[^\s<>"]*', response_text, re.IGNORECASE)
        
        # Count unique URLs (some may appear multiple times)
        has_direct_urls = len(jpg_urls) > 0 or len(png_urls) > 0 or len(webp_urls) > 0 or len(gif_urls) > 0
        
        # Debug logging for detection
        if len(jpg_urls) > 0 or len(png_urls) > 0 or len(webp_urls) > 0 or len(gif_urls) > 0:
            logger.debug(f"[Detection Debug] Found {len(jpg_urls)} JPG, {len(png_urls)} PNG, {len(webp_urls)} WebP, {len(gif_urls)} GIF URLs")
        
        characteristics = {
            'has_base64': 'data:image/jpeg;base64' in response_text or 'data:image/png;base64' in response_text,
            'has_setImagesSrc': '_setImagesSrc' in response_text,
            'has_deferred': 'data-deferred="1"' in response_text,
            'has_direct_urls': has_direct_urls,
            'has_gstatic': 'encrypted-tbn0.gstatic.com' in response_text,
            'has_jsdata': 'jsdata=' in response_text,
            'html_size': len(response_text),
        }
        
        # Determine response type based on characteristics (priority order)
        if characteristics['has_base64'] and characteristics['has_setImagesSrc']:
            response_type = 'TYPE_1_BASE64'
        elif characteristics['has_deferred']:
            response_type = 'TYPE_2_DEFERRED'
        elif characteristics['has_direct_urls']:
            response_type = 'TYPE_3_DIRECT_URLS'
        elif characteristics['has_gstatic']:
            response_type = 'TYPE_4_GSTATIC'
        elif characteristics['has_jsdata']:
            response_type = 'TYPE_5_JSDATA'
        else:
            response_type = 'TYPE_UNKNOWN'
        
        characteristics['response_type'] = response_type
        return response_type, characteristics

    def _search_google_images(self, query: str, max_results: int = 5, use_discogs_only: bool = False) -> List[str]:
        """
        Busca imágenes en Google Images (Last.fm disabled due to invalid API key).
        
        Args:
            query: Término de búsqueda (idealmente "artist album" o "artist title")
            max_results: Número máximo de resultados
            use_discogs_only: Ignored (kept for compatibility)
            
        Returns:
            Lista de URLs de imágenes encontradas
        """
        # Use Google Images search only (Last.fm API key is invalid)
        logger.debug(f"Buscando en Google Images: {query}")
        return self._search_google_images_discogs(query, max_results)
    
    def _search_google_images_single(self, query: str, max_results: int = 50) -> List[str]:
        """
        Búsqueda ÚNICA en Google Images - sin múltiples intentos.
        
        Used by dump_images_for_query to get images from a SINGLE query
        without trying multiple variations. This ensures debug.html matches
        the displayed URL.
        
        Args:
            query: Término de búsqueda completo (ya formateado)
            max_results: Número máximo de resultados
            
        Returns:
            Lista de URLs de imágenes encontradas
        """
        self._apply_delay()
        
        image_urls = []
        self._last_search_queries = []  # Reset tracking for this search
        
        try:
            logger.debug(f"Single search in Google Images: {query}")
            
            # Make ONE single request with the exact query provided
            url = "https://www.google.com/search"
            
            params = {
                "q": query,
                "udm": "2",  # Google Images  
                "hl": "en",  # Consistent English
                "tbs": "isz:m",  # Medium size preference
            }
            
            # Construir URL completa para debug
            from urllib.parse import urlencode
            full_url = f"{url}?{urlencode(params)}"
            logger.debug(f"Single search URL: {full_url}")
            
            # Track this query for debugging
            query_info = {
                'search_term': query,
                'full_url': full_url,
                'base_url': url,
                'params': params,
                'status': 'pending',
                'images_found': 0
            }
            
            try:
                # Add human-like delay
                self._apply_delay()
                
                response = self.session.get(
                    url,
                    params=params,
                    headers=self._get_headers(referrer="https://www.google.com/"),
                    proxies=self.proxies,
                    timeout=self.timeout,
                    allow_redirects=True
                )
                
                # Store the HTML response for inspection
                self._last_response_html = response.text
                
                query_info['status'] = f"HTTP {response.status_code}"
                query_info['final_url'] = response.url
                
                logger.debug(f"Response status: {response.status_code}")
                logger.debug(f"Final URL: {response.url}")
                
                # Detect rate limit
                if response.status_code == 429:
                    logger.error(f"Google rate limit: HTTP {response.status_code}")
                    query_info['status'] = "BLOCKED - Rate limit (429)"
                    self._last_search_queries.append(query_info)
                    raise Exception("429 Client Error: Too Many Requests")
                
                if '/sorry' in response.url:
                    logger.error(f"Google blocked search")
                    query_info['status'] = "BLOCKED - /sorry page"
                    self._last_search_queries.append(query_info)
                    raise Exception("Google blocked the search")
                
                response.raise_for_status()
                
                logger.debug(f"HTML response size: {len(response.text)} chars")
                
                # Extract images using the same strategy detection as main search
                response_type, characteristics = self._detect_response_type(response.text)
                logger.debug(f"Response type: {response_type}")
                
                extracted_images = []
                
                # Try base64 extraction first - use flexible pattern that validates
                logger.debug(f"Extracting base64 images")
                try:
                    # More flexible pattern: data:image/(jpeg|png|webp|gif);base64,XXXXX
                    base64_pattern = r"data:image/(?:jpeg|png|webp|gif|jpe);base64,[A-Za-z0-9+/]+={0,2}"
                    base64_matches = re.finditer(base64_pattern, response.text)
                    
                    base64_images = []
                    for match in base64_matches:
                        base64_url = match.group(0)
                        # Validate that base64 can be decoded before adding to list
                        try:
                            header, data = base64_url.split(',', 1)
                            # Try to decode to verify it's valid base64
                            base64.b64decode(data, validate=True)
                            base64_images.append(base64_url)
                        except Exception as e:
                            logger.debug(f"Skipping invalid base64: {e}")
                            continue
                    
                    if base64_images:
                        logger.debug(f"Found {len(base64_images)} valid base64 images")
                        extracted_images = base64_images
                except Exception as e:
                    logger.debug(f"Base64 extraction failed: {e}")
                
                # Fallback to direct URLs if needed
                if not extracted_images:
                    logger.debug(f"Trying direct URL extraction")
                    try:
                        jpg_urls = re.findall(r'https?://[^\s<>"]+\.(jpg|jpeg|jpe)[^\s<>"]*', response.text, re.IGNORECASE)
                        png_urls = re.findall(r'https?://[^\s<>"]+\.png[^\s<>"]*', response.text, re.IGNORECASE)
                        webp_urls = re.findall(r'https?://[^\s<>"]+\.webp[^\s<>"]*', response.text, re.IGNORECASE)
                        gif_urls = re.findall(r'https?://[^\s<>"]+\.gif[^\s<>"]*', response.text, re.IGNORECASE)
                        direct_urls = self._filter_image_urls(jpg_urls + png_urls + webp_urls + gif_urls)
                        if direct_urls:
                            logger.debug(f"Found {len(direct_urls)} direct URLs")
                            extracted_images = direct_urls
                    except Exception as e:
                        logger.debug(f"Direct URL extraction failed: {e}")
                
                # Limit to max_results
                image_urls = extracted_images[:max_results]
                query_info['images_found'] = len(image_urls)
                logger.debug(f"Returning {len(image_urls)} images")
                
            except Exception as e:
                logger.error(f"Search failed: {e}")
                query_info['status'] = f"ERROR: {str(e)}"
            
            finally:
                self._last_search_queries.append(query_info)
        
        except Exception as e:
            logger.error(f"Single search error: {e}")
            import traceback
            traceback.print_exc()
        
        return image_urls
    
    def _search_google_images_discogs(self, query: str, max_results: int = 5) -> List[str]:
        """
        Busca en Google Images y extrae URLs de imágenes reales.
        
        Google Images devuelve referencias a imágenes en múltiples formatos:
        - URLs directas a CDNs (Amazon, Wikimedia, etc.)
        - URLs a Google's gstatic (thumbnails comprimidos)
        - Enlaces a sitios origen
        
        Args:
            query: Término de búsqueda (artist album)
            max_results: Número máximo de resultados
            
        Returns:
            Lista de URLs de imágenes encontradas
        """
        self._apply_delay()
        
        image_urls = []
        self._last_search_queries = []  # Reset tracking for this search
        
        try:
            logger.debug(f"Buscando en Google Images: {query}")
            
            # Limpiar query
            clean_query = query.replace('"', '').strip()
            
            # Strategy: Build queries based on search_strategy
            # 'discogs' strategy includes "discogs" keyword
            # default/basic strategy uses generic terms
            if self.search_strategy == 'discogs':
                search_queries = [
                    f"{clean_query} discogs",           # Discogs-specific search
                    f"{clean_query} discogs cover",     # Specific to Discogs covers
                    f"{clean_query} album cover",       # Fallback to generic
                ]
            else:  # default/basic strategy
                search_queries = [
                    f"{clean_query}",                   # Basic search
                    f"{clean_query} album cover",       # Generic album cover
                    f"{clean_query} cover",             # Generic cover
                ]
            
            for search_term in search_queries:
                # Log the exact search query at DEBUG level (only shown with --debug)
                logger.debug(f"🔍 Searching Google Images: '{search_term}'")
                
                # Google Images con udm=2
                url = "https://www.google.com/search"
                
                # Randomize parameters to avoid bot detection
                # Google detects predictable patterns - randomizing makes requests look human-like
                
                # Randomize language parameter (simulate different users)
                languages = ["en", "es", "fr", "de", "it", "pt", "en-US", "en-GB"]
                hl_param = random.choice(languages)
                
                # Randomize image size preference (simulate different browsing behaviors)
                size_params = ["isz:l", "isz:m", "isz:s", ""]  # large, medium, small, or no size filter
                tbs_param = random.choice(size_params)
                
                params = {
                    "q": search_term,
                    "udm": "2",  # Google Images  
                    "hl": hl_param,  # RANDOMIZED language - avoids bot pattern detection
                    "tbs": tbs_param,  # RANDOMIZED size preference - avoids bot pattern detection
                }
                
                # Occasionally add start parameter for pagination (simulates browsing multiple pages)
                if random.random() < 0.15:  # Increased probability to 15% for more realistic browsing
                    params["start"] = random.choice([0, 10, 20, 30])  # Random page navigation
                
                # Occasionally add time-based filtering (simulates date-aware searches)
                if random.random() < 0.10:
                    params["tbs"] = (params.get("tbs", "") + ",qdr:m" if params.get("tbs") else "qdr:m")  # Last month
                
                # Construir URL completa para debug
                from urllib.parse import urlencode
                full_url = f"{url}?{urlencode(params)}"
                logger.debug(f"URL de búsqueda: {full_url}")
                
                # Track this query for debugging
                query_info = {
                    'search_term': search_term,
                    'full_url': full_url,
                    'base_url': url,
                    'params': params,
                    'status': 'pending',
                    'images_found': 0
                }
                
                try:
                    # Add human-like delay before this search
                    self._apply_delay()
                    
                    response = self.session.get(
                        url,
                        params=params,
                        headers=self._get_headers(referrer="https://www.google.com/"),
                        proxies=self.proxies,
                        timeout=self.timeout,
                        allow_redirects=True
                    )
                    
                    # Store the HTML response for inspection
                    self._last_response_html = response.text
                    
                    query_info['status'] = f"HTTP {response.status_code}"
                    query_info['final_url'] = response.url
                    
                    logger.debug(f"Respuesta de Google Images: {response.status_code}")
                    logger.debug(f"URL final: {response.url}")
                    
                    # Detect rate limit: HTTP 429 or redirect to /sorry
                    if response.status_code == 429:
                        logger.error(f"Google rejected search: HTTP {response.status_code} (Rate limit)")
                        query_info['status'] = "BLOCKED - Rate limit (429)"
                        self._last_search_queries.append(query_info)
                        raise Exception("429 Client Error: Too Many Requests - Google rate limit detected")
                    
                    if '/sorry' in response.url:
                        logger.error(f"Google redirigió a página de error: {response.url}")
                        query_info['status'] = "BLOCKED - Rate limit (/sorry)"
                        self._last_search_queries.append(query_info)
                        raise Exception("Google redirected to /sorry - Rate limit or blocking detected")
                    
                    response.raise_for_status()  # Lanzar para otros errores HTTP
                    
                    logger.debug(f"Respuesta HTML tamaño: {len(response.text)} caracteres")
                    
                    # ===== ADAPTIVE MULTI-STRATEGY EXTRACTION =====
                    # Detect Google's response type and apply appropriate extraction strategy
                    response_type, characteristics = self._detect_response_type(response.text)
                    logger.debug(f"Response type detected: {response_type}")
                    logger.debug(f"   Characteristics: base64={characteristics.get('has_base64')}, " +
                                f"setImagesSrc={characteristics.get('has_setImagesSrc')}, " +
                                f"deferred={characteristics.get('has_deferred')}, " +
                                f"direct_urls={characteristics.get('has_direct_urls')}, " +
                                f"gstatic={characteristics.get('has_gstatic')}")
                    
                    # Try extraction strategies in priority order
                    extracted_images = []
                    
                    # Strategy 1: TYPE_1_BASE64 - Embedded base64 images (best quality, Google's order)
                    if response_type == 'TYPE_1_BASE64':
                        logger.debug(f"Applying TYPE_1_BASE64 strategy (embedded base64 images)")
                        try:
                            # Use the original proven pattern that handles escaped characters
                            base64_pattern = r"var\s+s='(data:image/(?:jpeg|png);base64,[^']*)';\s*var\s+ii=\[[^\]]+\];\s*_setImagesSrc\(ii,s\);"
                            base64_matches = re.finditer(base64_pattern, response.text)
                            base64_images = [match.group(1) for match in base64_matches]
                            
                            if base64_images:
                                logger.debug(f"TYPE_1 extraction successful: {len(base64_images)} base64 images")
                                logger.debug(f"First image data length: {len(base64_images[0])} chars")
                                extracted_images = base64_images
                        except Exception as e:
                            logger.debug(f"TYPE_1 extraction failed: {e}")
                    
                    # Strategy 2: TYPE_2_DEFERRED - Lazy-loaded placeholders (use direct URL fallback)
                    elif response_type == 'TYPE_2_DEFERRED':
                        logger.debug(f"Applying TYPE_2_DEFERRED strategy (lazy-loading with URL fallback)")
                        # TYPE_2 has placeholder GIFs, but may have direct URLs elsewhere in HTML
                        try:
                            jpg_urls = re.findall(r'https?://[^\s<>"]+\.(jpg|jpeg|jpe)[^\s<>"]*', response.text, re.IGNORECASE)
                            png_urls = re.findall(r'https?://[^\s<>"]+\.png[^\s<>"]*', response.text, re.IGNORECASE)
                            webp_urls = re.findall(r'https?://[^\s<>"]+\.webp[^\s<>"]*', response.text, re.IGNORECASE)
                            gif_urls = re.findall(r'https?://[^\s<>"]+\.gif[^\s<>"]*', response.text, re.IGNORECASE)
                            direct_urls = self._filter_image_urls(jpg_urls + png_urls + webp_urls + gif_urls)
                            if direct_urls:
                                logger.debug(f"TYPE_2 extraction successful: {len(direct_urls)} direct URLs found")
                                extracted_images = direct_urls
                        except Exception as e:
                            logger.debug(f"TYPE_2 extraction failed: {e}")
                    
                    # Strategy 3: TYPE_3_DIRECT_URLS - Direct image URLs in HTML
                    elif response_type == 'TYPE_3_DIRECT_URLS':
                        logger.debug(f"Applying TYPE_3_DIRECT_URLS strategy (direct image URLs)")
                        try:
                            jpg_urls = re.findall(r'https?://[^\s<>"]+\.(jpg|jpeg|jpe)[^\s<>"]*', response.text, re.IGNORECASE)
                            png_urls = re.findall(r'https?://[^\s<>"]+\.png[^\s<>"]*', response.text, re.IGNORECASE)
                            webp_urls = re.findall(r'https?://[^\s<>"]+\.webp[^\s<>"]*', response.text, re.IGNORECASE)
                            gif_urls = re.findall(r'https?://[^\s<>"]+\.gif[^\s<>"]*', response.text, re.IGNORECASE)
                            direct_urls = self._filter_image_urls(jpg_urls + png_urls + webp_urls + gif_urls)
                            if direct_urls:
                                logger.debug(f"TYPE_3 extraction successful: {len(direct_urls)} direct URLs")
                                extracted_images = direct_urls
                        except Exception as e:
                            logger.debug(f"TYPE_3 extraction failed: {e}")
                    
                    # Strategy 4: TYPE_4_GSTATIC - Google's thumbnail service URLs
                    elif response_type == 'TYPE_4_GSTATIC':
                        logger.debug(f"Applying TYPE_4_GSTATIC strategy (Google thumbnail service URLs)")
                        try:
                            gstatic_pattern = r'https://encrypted-tbn0\.gstatic\.com/images\?[^\s"\'<>]*'
                            gstatic_urls = list(set(re.findall(gstatic_pattern, response.text)))
                            if gstatic_urls:
                                logger.debug(f"TYPE_4 extraction successful: {len(gstatic_urls)} gstatic URLs")
                                extracted_images = gstatic_urls
                        except Exception as e:
                            logger.debug(f"TYPE_4 extraction failed: {e}")
                    
                    # Strategy 5: TYPE_5_JSDATA - Encoded JavaScript data (fallback to direct URLs)
                    elif response_type == 'TYPE_5_JSDATA':
                        logger.debug(f"Applying TYPE_5_JSDATA strategy (encoded JS data, using URL fallback)")
                        # TYPE_5 requires JavaScript execution, so try direct URL extraction as fallback
                        try:
                            jpg_urls = re.findall(r'https?://[^\s<>"]+\.(jpg|jpeg|jpe)[^\s<>"]*', response.text, re.IGNORECASE)
                            png_urls = re.findall(r'https?://[^\s<>"]+\.png[^\s<>"]*', response.text, re.IGNORECASE)
                            webp_urls = re.findall(r'https?://[^\s<>"]+\.webp[^\s<>"]*', response.text, re.IGNORECASE)
                            gif_urls = re.findall(r'https?://[^\s<>"]+\.gif[^\s<>"]*', response.text, re.IGNORECASE)
                            direct_urls = self._filter_image_urls(jpg_urls + png_urls + webp_urls + gif_urls)
                            if direct_urls:
                                logger.debug(f"TYPE_5 fallback extraction successful: {len(direct_urls)} direct URLs")
                                extracted_images = direct_urls
                        except Exception as e:
                            logger.debug(f"TYPE_5 extraction failed: {e}")
                    
                    # Unknown type - try all strategies
                    else:
                        logger.debug(f"Applying TYPE_UNKNOWN strategy (trying all extraction methods)")
                        try:
                            # Try base64 first using the proven pattern
                            base64_pattern = r"var\s+s='(data:image/(?:jpeg|png);base64,[^']*)';\s*var\s+ii=\[[^\]]+\];\s*_setImagesSrc\(ii,s\);"
                            base64_matches = re.finditer(base64_pattern, response.text)
                            base64_images = [match.group(1) for match in base64_matches]
                            if base64_images:
                                extracted_images = base64_images
                            else:
                                # Try direct URLs
                                jpg_urls = re.findall(r'https?://[^\s<>"]+\.(jpg|jpeg|jpe)[^\s<>"]*', response.text, re.IGNORECASE)
                                png_urls = re.findall(r'https?://[^\s<>"]+\.png[^\s<>"]*', response.text, re.IGNORECASE)
                                webp_urls = re.findall(r'https?://[^\s<>"]+\.webp[^\s<>"]*', response.text, re.IGNORECASE)
                                gif_urls = re.findall(r'https?://[^\s<>"]+\.gif[^\s<>"]*', response.text, re.IGNORECASE)
                                direct_urls = self._filter_image_urls(jpg_urls + png_urls + webp_urls + gif_urls)
                                logger.debug(f"[TYPE_UNKNOWN Debug] Found {len(jpg_urls)} JPG, {len(png_urls)} PNG, {len(webp_urls)} WebP, {len(gif_urls)} GIF URLs, filtered to {len(direct_urls)}")
                                
                                # Save HTML for analysis if no URLs found
                                if len(direct_urls) == 0 and len(jpg_urls) == 0:
                                    debug_file = f"debug_no_urls_{search_term[:20].replace(' ', '_')}.html"
                                    try:
                                        with open(debug_file, 'w', encoding='utf-8') as f:
                                            f.write(response.text[:100000])  # First 100KB
                                        logger.debug(f"Saved HTML sample to {debug_file} for analysis")
                                    except:
                                        pass
                                
                                if direct_urls:
                                    extracted_images = direct_urls
                                else:
                                    # Try gstatic
                                    gstatic_pattern = r'https://encrypted-tbn0\.gstatic\.com/images\?[^\s<>"]*'
                                    gstatic_urls = list(set(re.findall(gstatic_pattern, response.text)))
                                    if gstatic_urls:
                                        extracted_images = gstatic_urls
                            if extracted_images:
                                logger.debug(f"TYPE_UNKNOWN extraction successful: {len(extracted_images)} images found")
                        except Exception as e:
                            logger.debug(f"TYPE_UNKNOWN extraction failed: {e}")
                    
                    # Return results if we found images
                    if extracted_images:
                        image_urls = extracted_images[:max_results]
                        logger.debug(f"Retornando {len(image_urls)} imagenes para: '{search_term}'")
                        query_info['status'] = f"SUCCESS ({response_type})"
                        query_info['images_found'] = len(image_urls)
                        query_info['response_type'] = response_type
                        self._last_search_queries.append(query_info)
                        return image_urls
                    
                    # No images found with any strategy
                    logger.debug(f"No se encontraron imagenes en Google Images para: {search_term}")
                    logger.debug(f"  Tipo de respuesta: {response_type} | Tamano HTML: {len(response.text)} bytes")
                    query_info['status'] = f"NO_IMAGES ({response_type})"
                    query_info['html_size'] = len(response.text)
                    query_info['response_type'] = response_type
                    self._last_search_queries.append(query_info)
                    # Continue to next search query variation
                    continue
                        
                except Exception as e:
                    # RE-LANZAR errores de 429 para que sea capturado por search_album/search_single
                    if "429" in str(e):
                        raise
                    logger.debug(f"Error en búsqueda de Google Images: {e}")
                    query_info['status'] = f"ERROR: {str(e)[:100]}"
                    self._last_search_queries.append(query_info)
                    logger.warning(f"⚠️ Error searching Google Images for '{search_term}': {str(e)[:100]}")
                    # Continue to next search query variation
                    continue
        
        except Exception as e:
            # RE-LANZAR errores de 429 para que sea capturado por search_album/search_single
            if "429" in str(e):
                raise
            logger.debug(f"Error buscando en Google Images: {e}")
            logger.warning(f"⚠️ Google Images search error: {str(e)[:100]}")
        
        # No encontró imágenes en ninguno de los intentos
        logger.debug(f"No se encontraron imágenes en Google Images para: {query}")
        return []
    
    def _download_image(self, image_url: str, min_size: int = 50, apply_delay: bool = True) -> Optional[ImageData]:
        """
        Descarga una imagen desde una URL o decodifica una data URL en base64.
        
        Args:
            image_url: URL de la imagen o data URL en base64
            min_size: Tamaño mínimo en píxeles (por defecto 50)
            apply_delay: Si aplica delay antes de procesar (desactivar para base64 en-memoria)
            
        Returns:
            ImageData si la descarga fue exitosa, None en caso contrario
        """
        # Solo aplicar delay si es una URL HTTP (no para base64 en-memoria)
        if apply_delay and not image_url.startswith('data:'):
            self._apply_delay()
        
        try:
            # Verificar si es una data URL en base64
            if image_url.startswith('data:image/'):
                logger.debug(f"Procesando data URL en base64 (tamaño: {len(image_url)} caracteres)")
                
                # Extraer el tipo MIME y los datos base64
                import base64
                
                # Formato: data:image/jpeg;base64,/9j/4AAQSkZJRgAB...
                header, data = image_url.split(',', 1)
                
                # Decodificar base64 with 3-tier fallback strategy for corrupted/truncated data
                image_data = None
                try:
                    # Strategy 1: Try as-is
                    image_data = base64.b64decode(data)
                except Exception as e1:
                    try:
                        # Strategy 2: Fix padding (add '=' for multiple of 4)
                        padding = len(data) % 4
                        if padding:
                            data_padded = data + ('=' * (4 - padding))
                        else:
                            data_padded = data
                        image_data = base64.b64decode(data_padded)
                    except Exception as e2:
                        try:
                            # Strategy 3: Clean non-base64 chars, then fix padding
                            import re as regex_module
                            data_clean = regex_module.sub(r'[^A-Za-z0-9+/=]', '', data)
                            padding = len(data_clean) % 4
                            if padding:
                                data_clean = data_clean + ('=' * (4 - padding))
                            image_data = base64.b64decode(data_clean)
                        except Exception as e3:
                            logger.debug(f"Error decodificando base64 (3 estrategias fallidas): {e1}")
                            return None
                
                if not image_data:
                    logger.debug(f"Error: No image data after 3 decode strategies")
                    return None
                
                # Validar que es una imagen válida
                try:
                    img = Image.open(BytesIO(image_data))
                    width, height = img.size
                    
                    # Descartar imágenes muy pequeñas (configurable)
                    if width < min_size or height < min_size:
                        logger.debug(f"Imagen muy pequeña (descartada): {width}x{height} - {len(image_data)} bytes (min: {min_size}x{min_size})")
                        return None
                    
                    logger.debug(f"Imagen válida decodificada: {width}x{height}")
                    
                    return ImageData(
                        data=image_data,
                        url=image_url[:80] + "..." if len(image_url) > 80 else image_url,
                        width=width,
                        height=height,
                        source="base64"
                    )
                except Exception as e:
                    logger.debug(f"Error validando imagen base64: {e}")
                    return None
            
            # Caso normal: descargar desde URL HTTP(S)
            headers = self._get_headers()
            
            logger.debug(f"Descargando imagen: {image_url[:80]}...")
            
            response = self.session.get(
                image_url,
                headers=headers,
                proxies=self.proxies,
                timeout=self.timeout,
                allow_redirects=True
            )
            
            # Detect rate limit
            if response.status_code == 429:
                logger.error(f"Google rejected download: HTTP 429 (Rate limit)")
                raise Exception("429 Client Error: Too Many Requests")
            
            response.raise_for_status()
            
            # Validar que es una imagen
            content_type = response.headers.get('content-type', '').lower()
            if 'image' not in content_type:
                logger.debug(f"Content-Type no es imagen: {content_type}")
                return None
            
            # Validar tamaño (mínimo 1KB)
            if len(response.content) < 1 * 1024:
                logger.debug(f"Imagen muy pequeña: {len(response.content)} bytes")
                return None
            
            # Validar que es una imagen válida
            try:
                img = Image.open(BytesIO(response.content))
                width, height = img.size
                
                # Descartar imágenes muy pequeñas o muy grandes
                if width < min_size or height < min_size:
                    logger.debug(f"Imagen muy pequeña: {width}x{height} (min: {min_size}x{min_size})")
                    return None
                if width > 10000 or height > 10000:
                    logger.debug(f"Imagen muy grande: {width}x{height}")
                    return None
                
                logger.debug(f"Imagen válida descargada: {width}x{height} desde {image_url[:60]}...")
                
                return ImageData(
                    data=response.content,
                    url=image_url,
                    width=width,
                    height=height
                )
            except Exception as e:
                logger.debug(f"Error validando imagen: {e}")
                return None
                
        except requests.exceptions.RequestException as e:
            logger.debug(f"Error descargando imagen: {e}")
            return None
    
    def search_album(self, artist: str, album: str, year: Optional[int] = None) -> Optional[ImageData]:
        """
        Busca imagen de álbum en Google Images.
        
        Estrategia según search_strategy:
        - 'lastfm': Busca en Last.fm, fallback a Google Images
        - 'discogs': Busca solo en Google Images con "discogs" en la query
        
        Maneja automáticamente errores de rate limit (HTTP 429) con:
        - Sleep exponencial (5 min, 10 min, 20 min, etc.)
        - Reintentos automáticos después del sleep
        - Logging continuo de progreso
        
        Args:
            artist: Nombre del artista
            album: Nombre del álbum
            year: Año opcional
            
        Returns:
            ImageData con la imagen descargada, o None si no se encuentra
        """
        from .state_manager import ProcessingState
        
        use_discogs_only = (self.search_strategy == 'discogs')
        
        queries = []
        
        # Estrategia 1: Artist - Album (Year)
        if year:
            queries.append(f'"{artist}" "{album}" {year}')
        
        # Estrategia 2: Artist Album
        queries.append(f'"{artist}" "{album}"')
        queries.append(f'{artist} {album} album')
        
        # Estrategia 3: Album
        queries.append(f'"{album}"')
        
        # RANDOMIZE query order to avoid predictable patterns
        # Google detects when the same search pattern is tried in the same order
        random.shuffle(queries)
        
        logger.debug(f"Buscando álbum: {artist} - {album} (año: {year}), estrategia: {self.search_strategy}")
        logger.debug(f"Query order (randomized): {queries}")
        
        for idx, query in enumerate(queries):
            logger.debug(f"Intentando búsqueda [{idx+1}/{len(queries)}]: {query}")
            
            # Retry loop for rate limits
            max_retries = 3
            attempt = 0
            
            while attempt < max_retries:
                try:
                    image_urls = self._search_google_images(query, max_results=20, use_discogs_only=use_discogs_only)
                    
                    # TWO-PASS STRATEGY:
                    # Pass 1: Try images >= 50x50 (preferred quality)
                    logger.debug(f"Pass 1: Looking for images >= 50x50 pixels")
                    for image_url in image_urls:
                        image_data = self._download_image(image_url, min_size=50)
                        if image_data:
                            logger.info(f"Imagen de álbum encontrada: {artist} - {album} ({image_data.width}x{image_data.height})")
                            # Eliminar estado si existe (búsqueda completada)
                            self.state_manager.delete_state(artist, album=album, query_type="album")
                            self._rate_limit_hits = 0  # Reset counter on success
                            return image_data
                    
                    # Pass 2: Relax filter - try images >= 20x20 (accept small logos/thumbnails)
                    logger.debug(f"Pass 2: Relaxing filter to images >= 20x20 pixels")
                    for image_url in image_urls:
                        image_data = self._download_image(image_url, min_size=20)
                        if image_data:
                            logger.info(f"Imagen de álbum encontrada (pequeña): {artist} - {album} ({image_data.width}x{image_data.height})")
                            # Eliminar estado si existe (búsqueda completada)
                            self.state_manager.delete_state(artist, album=album, query_type="album")
                            self._rate_limit_hits = 0  # Reset counter on success
                            return image_data
                    
                    # No image found in this query, try next
                    break
                
                except Exception as e:
                    # Detectar si es un bloqueo de Google
                    error_msg = str(e)
                    if "429" in error_msg or "/sorry" in error_msg or "too many" in error_msg.lower():
                        attempt += 1
                        
                        if attempt < max_retries:
                            # Silent retry - no need to log, exponential backoff handles it
                            self._handle_rate_limit(query)
                            logger.info(f"Retrying search [{attempt}/{max_retries}]...")
                            continue
                        else:
                            logger.error(f"Max retries ({max_retries}) alcanzados. Google is blocking this search.")
                            logger.warning(f"Sleeping for exponential backoff, then will retry on next run...")
                            # Don't save blocked state - let it retry naturally on next attempt
                            # This allows exponential backoff to work correctly
                            return None
                    else:
                        logger.debug(f"Error en búsqueda: {e}")
                        break
        
        logger.warning(f"No image found for album: {artist} - {album}")
        return None
    
    def search_single(self, artist: str, title: str) -> Optional[ImageData]:
        """
        Busca imagen de canción individual en Google Images.
        
        Estrategia según search_strategy:
        - 'lastfm': Busca en Last.fm
        - 'discogs': Busca solo en Google Images con "discogs" en la query
        
        Maneja automáticamente errores de rate limit (HTTP 429) con:
        - Sleep exponencial (5 min, 10 min, 20 min, etc.)
        - Reintentos automáticos después del sleep
        - Logging continuo de progreso
        
        Args:
            artist: Nombre del artista
            title: Nombre de la canción
            
        Returns:
            ImageData con la imagen descargada, o None si no se encuentra
        """
        from .state_manager import ProcessingState
        
        use_discogs_only = (self.search_strategy == 'discogs')
        
        queries = [
            f'"{artist}" "{title}"',
            f'{artist} {title} single',
            f'"{title}" official',
        ]
        
        logger.debug(f"Buscando single: {artist} - {title}, estrategia: {self.search_strategy}")
        
        for idx, query in enumerate(queries):
            logger.debug(f"Intentando búsqueda [{idx+1}/{len(queries)}]: {query}")
            
            # Retry loop for rate limits
            max_retries = 3
            attempt = 0
            
            while attempt < max_retries:
                try:
                    image_urls = self._search_google_images(query, max_results=10, use_discogs_only=use_discogs_only)
                    
                    for image_url in image_urls:
                        image_data = self._download_image(image_url)
                        if image_data:
                            logger.info(f"Imagen de single encontrada: {artist} - {title}")
                            # Eliminar estado si existe (búsqueda completada)
                            self.state_manager.delete_state(artist, title=title, query_type="single")
                            self._rate_limit_hits = 0  # Reset counter on success
                            return image_data
                    
                    # No image found in this query, try next
                    break
                
                except Exception as e:
                    # Detectar si es un bloqueo de Google
                    error_msg = str(e)
                    if "429" in error_msg or "/sorry" in error_msg or "too many" in error_msg.lower():
                        attempt += 1
                        
                        if attempt < max_retries:
                            # Silent retry - no need to log, exponential backoff handles it
                            self._handle_rate_limit(query)
                            logger.info(f"Retrying search [{attempt}/{max_retries}]...")
                            continue
                        else:
                            logger.error(f"Max retries ({max_retries}) alcanzados. Google is blocking this search.")
                            logger.warning(f"Sleeping for exponential backoff, then will retry on next run...")
                            # Don't save blocked state - let it retry naturally on next attempt
                            # This allows exponential backoff to work correctly
                            return None
                    else:
                        logger.debug(f"Error en búsqueda: {e}")
                        break
        
        logger.warning(f"No image found for single: {artist} - {title}")
        return None
    
    def close(self):
        """Cierra la sesión HTTP."""
        self.session.close()
        logger.debug("GoogleImageScraper sesión cerrada")
    
    def __enter__(self):
        """Context manager entry."""
        return self
    
    def __exit__(self, exc_type, exc_val, exc_tb):
        """Context manager exit."""
        self.close()
    
    def dump_images_for_query(self, query: str, output_dir: Path) -> int:
        """
        Extrae todas las imágenes encontradas para una consulta y las guarda en archivos.
        Cada archivo tiene un nombre con formato: NNN-quality.EXT
        Ej: 001-small.jpg, 002-medium.jpg, 003-large.png
        
        Además crea un archivo index.html para ver todas las imágenes.
        
        Args:
            query: Término de búsqueda
            output_dir: Directorio donde guardar las imágenes
            
        Returns:
            Número de imágenes guardadas
        """
        from pathlib import Path
        import base64
        from PIL import Image
        from io import BytesIO
        
        # Crear directorio si no existe
        output_dir = Path(output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)
        
        logger.info(f"Dumping images for query: {query}")
        logger.info(f"Output directory: {output_dir}")
        
        try:
            # Hacer búsqueda ÚNICA (sin múltiples estrategias)
            # Esto asegura que el debug.html corresponde a la URL mostrada
            image_urls = self._search_google_images_single(query, max_results=50)
            
            if not image_urls:
                logger.warning(f"No images found for query: {query}")
                return 0
            
            logger.info(f"Found {len(image_urls)} image URLs")
            
            # EXTRACT IMAGES FROM HTML RESPONSE FIRST (before Pass 1/Pass 2)
            # Get base64 images embedded in the HTML
            html_response = self._last_response_html if hasattr(self, '_last_response_html') else ""
            base64_images_extracted = []
            
            if html_response:
                logger.debug(f"Extracting base64 images from HTML response ({len(html_response)} chars)")
                
                # Try to extract all base64 image data URLs from the HTML
                # Pattern: data:image/(jpeg|png|webp|gif);base64,XXXXX
                # This is more flexible than the previous pattern
                base64_pattern = r"data:image/(?:jpeg|png|webp|gif|jpe);base64,[A-Za-z0-9+/]+={0,2}"
                base64_matches = re.finditer(base64_pattern, html_response)
                
                for match in base64_matches:
                    base64_url = match.group(0)
                    # Validate that base64 can be decoded before adding to list
                    try:
                        header, data = base64_url.split(',', 1)
                        # Fix padding issues: base64 should be multiple of 4 chars
                        # This allows incomplete base64 strings to be decoded
                        padding = len(data) % 4
                        if padding:
                            data_padded = data + ('=' * (4 - padding))
                        else:
                            data_padded = data
                        # Try to decode to verify it's valid base64 (without validate=True to allow truncated data)
                        base64.b64decode(data_padded)
                        base64_images_extracted.append(base64_url)  # Store original
                    except Exception as e:
                        logger.debug(f"Skipping invalid base64 {match.group(0)[:80]}...: {e}")
                        continue
            
            if base64_images_extracted:
                logger.debug(f"Found {len(base64_images_extracted)} valid base64 images in HTML response")
            
            # MERGE both sources for Pass 1/Pass 2 selection
            # Use all extracted base64 images PLUS image_urls from _search_google_images_single
            all_images_for_selection = base64_images_extracted + image_urls
            logger.debug(f"Total images for Pass 1/Pass 2 selection: {len(all_images_for_selection)}")
            
            # Mostrar URLs de búsqueda que se utilizaron
            search_queries = self.get_last_search_queries()
            if search_queries:
                print("\n" + "=" * 80)
                print(f"Google Images Search Queries Used:")
                print("=" * 80)
                for i, query_info in enumerate(search_queries, 1):
                    print(f"\n[Query {i}]")
                    print(f"  Search Term: {query_info.get('search_term', 'N/A')}")
                    print(f"  Full URL: {query_info.get('full_url', 'N/A')}")
                    print(f"  Images Found: {query_info.get('images_found', 0)}")
                print("\n" + "=" * 80 + "\n")
            
            saved_count = 0
            saved_images = []  # Para HTML index
            
            # FIND AND SAVE THE "BEST" IMAGE (using 2-pass strategy)
            # This is what would be used for folder.jpg
            selected_image_data = None
            selected_image_url = None
            selected_pass = None
            
            # Pass 1: Try images >= 50x50 pixels (preferred quality)
            logger.debug(f"Pass 1: Looking for images >= 50x50 pixels")
            for idx, image_url in enumerate(all_images_for_selection, 1):
                # Skip delays for base64 images (they're in-memory, not HTTP requests)
                img_data = self._download_image(image_url, min_size=50, apply_delay=False)
                if img_data:
                    selected_image_data = img_data
                    selected_image_url = image_url
                    selected_pass = 1
                    logger.debug(f"Pass 1: Selected image #{idx} ({img_data.width}x{img_data.height})")
                    break
            
            # Pass 2: If Pass 1 found nothing, try >= 20x20 pixels (fallback)
            if not selected_image_data:
                logger.debug(f"Pass 2: Looking for images >= 20x20 pixels (Pass 1 found nothing)")
                for idx, image_url in enumerate(all_images_for_selection, 1):
                    # Skip delays for base64 images (they're in-memory, not HTTP requests)
                    img_data = self._download_image(image_url, min_size=20, apply_delay=False)
                    if img_data:
                        selected_image_data = img_data
                        selected_image_url = image_url
                        selected_pass = 2
                        logger.debug(f"Pass 2: Selected image #{idx} ({img_data.width}x{img_data.height})")
                        break
            
            # Save the selected image as 00-selected-image.{ext}
            if selected_image_data:
                # Determine format
                try:
                    img_pil = Image.open(BytesIO(selected_image_data.data))
                    img_format = img_pil.format or "jpeg"
                    if img_format.lower() == "jpeg":
                        ext = "jpg"
                    elif img_format.lower() == "png":
                        ext = "png"
                    elif img_format.lower() == "webp":
                        ext = "webp"
                    elif img_format.lower() == "gif":
                        ext = "gif"
                    else:
                        ext = "jpg"
                    
                    selected_filename = f"00-selected-image.{ext}"
                    selected_filepath = output_dir / selected_filename
                    
                    with open(selected_filepath, 'wb') as f:
                        f.write(selected_image_data.data)
                    
                    size_kb = len(selected_image_data.data) / 1024
                    print(f"\n{'='*80}")
                    print(f"SELECTED IMAGE (Pass {selected_pass}):")
                    print(f"{'='*80}")
                    print(f"  File: {selected_filename}")
                    print(f"  Dimensions: {selected_image_data.width}x{selected_image_data.height} pixels")
                    print(f"  Size: {size_kb:.1f} KB")
                    print(f"  Format: {img_format}")
                    print(f"{'='*80}\n")
                    
                    saved_count += 1
                    saved_images.append({
                        'idx': 0,
                        'filename': selected_filename,
                        'width': selected_image_data.width,
                        'height': selected_image_data.height,
                        'size_kb': size_kb,
                        'quality': 'selected',
                        'pass': selected_pass
                    })
                except Exception as e:
                    logger.warning(f"Could not save selected image: {e}")
            
            # FOR DUMP MODE: Process ALL base64 images from extraction (no HTTP downloads)
            # This avoids multiple requests to download images - keeping consistent with single Google request
            logger.debug(f"Processing {len(base64_images_extracted)} extracted base64 images (no HTTP downloads for consistency)")
            
            for idx, image_url in enumerate(base64_images_extracted, 1):
                try:
                    # All images in dump mode are base64 (no HTTP downloads)
                    image_format = "jpg"
                    
                    # Extraer formato
                    if 'png' in image_url.lower():
                        image_format = 'png'
                    elif 'jpeg' in image_url.lower() or 'jpg' in image_url.lower():
                        image_format = 'jpg'
                    elif 'webp' in image_url.lower():
                        image_format = 'webp'
                    elif 'gif' in image_url.lower():
                        image_format = 'gif'
                    
                    # Decodificar base64
                    header, data = image_url.split(',', 1)
                    image_data = None
                    
                    # Try decode with multiple strategies (to handle padding/corruption issues)
                    try:
                        # First, try as-is
                        image_data = base64.b64decode(data)
                    except:
                        try:
                            # Strategy 2: Fix padding
                            padding = len(data) % 4
                            if padding:
                                data_padded = data + ('=' * (4 - padding))
                            else:
                                data_padded = data
                            image_data = base64.b64decode(data_padded)
                        except:
                            try:
                                # Strategy 3: Remove non-base64 chars and fix padding
                                import re as regex_module
                                data_clean = regex_module.sub(r'[^A-Za-z0-9+/=]', '', data)
                                padding = len(data_clean) % 4
                                if padding:
                                    data_clean = data_clean + ('=' * (4 - padding))
                                image_data = base64.b64decode(data_clean)
                            except Exception as e:
                                logger.debug(f"[{idx:03d}] Skipping after 3 decode strategies: {type(e).__name__}")
                                continue
                    
                    if not image_data:
                        logger.debug(f"[{idx:03d}] No image data decoded")
                        continue
                    
                    # Validar y obtener dimensiones
                    try:
                        img = Image.open(BytesIO(image_data))
                        width, height = img.size
                        size_kb = len(image_data) / 1024
                        
                        # Skip tracking pixels and other useless 1x1 or 2x2 images
                        if (width <= 2 and height <= 2) or size_kb < 0.1:
                            logger.debug(f"[{idx:03d}] Skipping tracking pixel: {width}x{height}, {size_kb:.1f}KB")
                            continue
                        
                        # Determinar calidad basado en dimensiones y tamaño
                        if width >= 300 and height >= 300 and size_kb >= 50:
                            quality = "large"
                        elif width >= 100 and height >= 100 and size_kb >= 10:
                            quality = "medium"
                        else:
                            quality = "small"
                        
                        # Nombre del archivo: NNN-quality.ext
                        filename = f"{idx:03d}-{quality}.{image_format}"
                        filepath = output_dir / filename
                        
                        # Guardar imagen
                        with open(filepath, 'wb') as f:
                            f.write(image_data)
                        
                        logger.info(f"[{idx:03d}] Saved: {filename} ({width}x{height}, {size_kb:.1f}KB, {quality})")
                        saved_count += 1
                        saved_images.append({
                            'idx': idx,
                            'filename': filename,
                            'width': width,
                            'height': height,
                            'size_kb': size_kb,
                            'quality': quality
                        })
                        
                    except Exception as e:
                        logger.warning(f"[{idx:03d}] Failed to validate/save image: {type(e).__name__}: {e}")
                        continue
                
                except Exception as e:
                    logger.warning(f"[{idx:03d}] Unexpected error: {type(e).__name__}: {e}")
                    continue
            
            # Crear HTML index
            html_content = self._generate_image_index_html(query, saved_images)
            index_path = output_dir / "index.html"
            with open(index_path, 'w', encoding='utf-8') as f:
                f.write(html_content)
            logger.info(f"Created index: {index_path}")
            
            logger.info(f"Successfully saved {saved_count} base64 images from single Google search (no extra HTTP requests)")
            return saved_count
            
        except Exception as e:
            logger.error(f"Error dumping images: {e}")
            return 0
    
    def _generate_image_index_html(self, query: str, images: list) -> str:
        """Genera un archivo HTML con index de todas las imágenes guardadas."""
        html = f"""<!DOCTYPE html>
<html>
<head>
    <meta charset="UTF-8">
    <title>Image Dump: {query}</title>
    <style>
        body {{
            font-family: Arial, sans-serif;
            margin: 20px;
            background: #f5f5f5;
        }}
        h1 {{
            color: #333;
        }}
        .stats {{
            background: #e3f2fd;
            padding: 15px;
            border-radius: 5px;
            margin-bottom: 20px;
        }}
        .image-grid {{
            display: grid;
            grid-template-columns: repeat(auto-fill, minmax(250px, 1fr));
            gap: 20px;
            margin-top: 20px;
        }}
        .image-card {{
            background: white;
            border-radius: 5px;
            overflow: hidden;
            box-shadow: 0 2px 4px rgba(0,0,0,0.1);
            transition: transform 0.2s;
        }}
        .image-card:hover {{
            transform: scale(1.02);
            box-shadow: 0 4px 8px rgba(0,0,0,0.2);
        }}
        .image-container {{
            display: flex;
            align-items: center;
            justify-content: center;
            background: #f0f0f0;
            height: 200px;
            overflow: hidden;
        }}
        .image-container img {{
            max-width: 100%;
            max-height: 100%;
            object-fit: contain;
        }}
        .image-info {{
            padding: 10px;
        }}
        .image-number {{
            font-weight: bold;
            color: #1976d2;
            font-size: 14px;
        }}
        .image-filename {{
            font-size: 12px;
            color: #666;
            word-break: break-word;
            margin: 5px 0;
        }}
        .image-dimensions {{
            font-size: 11px;
            color: #999;
        }}
        .quality-badge {{
            display: inline-block;
            padding: 3px 8px;
            border-radius: 3px;
            font-size: 10px;
            font-weight: bold;
            margin-top: 5px;
        }}
        .quality-large {{
            background: #4caf50;
            color: white;
        }}
        .quality-medium {{
            background: #ff9800;
            color: white;
        }}
        .quality-small {{
            background: #f44336;
            color: white;
        }}
    </style>
</head>
<body>
    <h1>Image Dump: {query}</h1>
    <div class="stats">
        <p><strong>Total images:</strong> {len(images)}</p>
        <p><strong>Query:</strong> {query}</p>
        <p><strong>Note:</strong> Images are numbered in extraction order. The first valid image (not padded) is usually #006-#010.</p>
    </div>
    <div class="image-grid">
"""
        
        for img in images:
            quality_class = f"quality-{img['quality']}"
            html += f"""        <div class="image-card">
            <div class="image-container">
                <img src="{img['filename']}" alt="Image {img['idx']}">
            </div>
            <div class="image-info">
                <div class="image-number">#{img['idx']:03d}</div>
                <div class="image-filename">{img['filename']}</div>
                <div class="image-dimensions">{img['width']}x{img['height']} · {img['size_kb']:.1f}KB</div>
                <div class="quality-badge {quality_class}">{img['quality'].upper()}</div>
            </div>
        </div>
"""
        
        html += """    </div>
</body>
</html>
"""
        return html
