"""Debug output utilities for MUSEE CLI."""

import click
from typing import List, Optional


class DebugOutput:
    """Utilidades para mostrar información de debug de forma clara."""
    
    @staticmethod
    def print_search_debug(search_type: str, query: str, results: int, 
                          options: Optional[List[tuple]] = None) -> None:
        """Imprime información de debug sobre una búsqueda.
        
        Args:
            search_type: Tipo de búsqueda ('single' o 'album')
            query: Query enviada a la API
            results: Número de resultados encontrados
            options: Lista de tuplas (title, year, has_image)
        """
        click.echo()
        click.echo("=" * 80)
        click.echo(f"[DEBUG] {search_type.upper()} SEARCH")
        click.echo("=" * 80)
        click.echo(f"Query: {query}")
        click.echo(f"Results: {results}")
        
        if options:
            click.echo()
            click.echo("Options found:")
            for idx, (title, year, has_image) in enumerate(options, 1):
                status = "✓" if has_image else "✗"
                year_str = f"({year})" if year else ""
                click.echo(f"  [{idx}] {status} {title} {year_str}")
        else:
            click.echo(click.style("  No results found", fg="red"))
        
        click.echo()

    @staticmethod
    def print_search_fallback(reason: str) -> None:
        """Imprime información cuando hay fallback en la búsqueda.
        
        Args:
            reason: Razón del fallback
        """
        click.echo(click.style(f"[INFO] Fallback: {reason}", fg="yellow"))
