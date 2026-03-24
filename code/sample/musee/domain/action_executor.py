"""Action orchestration and sequential execution."""

from typing import List, Optional, Callable, Dict, Any
from pathlib import Path
from enum import Enum
import click


class ActionType(Enum):
    """Enum for action types."""
    NORMALIZE_FILES = "normalize_files"
    SCRAP_FOLDERS = "scrap_folders"
    SCRAP_SONGS = "scrap_songs"
    GENERATE_PLAYLISTS = "generate_playlists"
    REVERT_RENAMES = "revert_renames"
    REVERT_IMAGES = "revert_images"
    CLEANUP_IMAGES = "cleanup_images"


class ActionExecutor:
    """Orchestrates execution of multiple actions in proper order."""
    
    # Define action ordering (lower values execute first)
    ACTION_ORDER = {
        ActionType.NORMALIZE_FILES: 1,
        ActionType.SCRAP_FOLDERS: 2,
        ActionType.SCRAP_SONGS: 3,
        ActionType.GENERATE_PLAYLISTS: 4,
        ActionType.REVERT_RENAMES: 5,
        ActionType.REVERT_IMAGES: 5,
        ActionType.CLEANUP_IMAGES: 5,
    }
    
    # Actions that cannot be combined with others
    EXCLUSIVE_ACTIONS = {
        ActionType.REVERT_RENAMES,
        ActionType.REVERT_IMAGES,
        ActionType.CLEANUP_IMAGES,
    }
    
    def __init__(self):
        """Initialize action executor."""
        self.actions: List[ActionType] = []
        self.callbacks: Dict[ActionType, Callable] = {}
        self.skip_single_file: bool = False
        self.progress_callback: Optional[Callable] = None
    
    def add_action(self, action: ActionType) -> None:
        """Add an action to the execution queue.
        
        Args:
            action: ActionType to add
            
        Raises:
            ValueError: If action combination is invalid
        """
        if action in self.EXCLUSIVE_ACTIONS and self.actions:
            raise ValueError(
                f"Action {action.value} cannot be combined with other actions. "
                f"Current actions: {[a.value for a in self.actions]}"
            )
        
        if self.actions and any(a in self.EXCLUSIVE_ACTIONS for a in self.actions):
            raise ValueError(
                f"Cannot add action {action.value} when exclusive actions are already added. "
                f"Current actions: {[a.value for a in self.actions]}"
            )
        
        if action not in self.actions:
            self.actions.append(action)
    
    def remove_action(self, action: ActionType) -> None:
        """Remove an action from the execution queue.
        
        Args:
            action: ActionType to remove
        """
        if action in self.actions:
            self.actions.remove(action)
    
    def set_callback(self, action: ActionType, callback: Callable) -> None:
        """Set a callback for an action.
        
        Args:
            action: ActionType
            callback: Callable that executes the action
        """
        self.callbacks[action] = callback
    
    def set_progress_callback(self, callback: Callable) -> None:
        """Set progress callback.
        
        Args:
            callback: Callable for progress updates
        """
        self.progress_callback = callback
    
    def get_ordered_actions(self) -> List[ActionType]:
        """Get actions in proper execution order.
        
        Returns:
            List of ActionTypes sorted by execution order
        """
        return sorted(self.actions, key=lambda a: self.ACTION_ORDER.get(a, 999))
    
    def execute(self, path: Path, options: Dict[str, Any]) -> Dict[str, Any]:
        """Execute all actions in proper order.
        
        Args:
            path: Path to process
            options: Options dict with parameters for each action
                    Keys like: force, dry_run, debug, image_source, etc.
                    
        Returns:
            Dict with results from each action
        """
        if not self.actions:
            raise ValueError("No actions configured")
        
        if not self.callbacks:
            raise ValueError("No callbacks registered")
        
        results = {}
        ordered_actions = self.get_ordered_actions()
        total_actions = len(ordered_actions)
        
        click.echo()
        click.echo("=" * 80)
        click.echo(click.style("EXECUTION PLAN", fg="cyan", bold=True))
        click.echo("=" * 80)
        for i, action in enumerate(ordered_actions, 1):
            click.echo(f"{i}. {action.value}")
        click.echo("=" * 80)
        click.echo()
        
        for i, action in enumerate(ordered_actions, 1):
            if action not in self.callbacks:
                raise ValueError(f"No callback registered for action: {action.value}")
            
            click.echo()
            click.echo("=" * 80)
            click.echo(click.style(f"STEP {i}/{total_actions}: {action.value.upper()}", fg="yellow", bold=True))
            click.echo("=" * 80)
            click.echo()
            
            try:
                callback = self.callbacks[action]
                result = callback(path, action, options)
                results[action.value] = result
            except Exception as e:
                click.echo(click.style(f"ERROR in {action.value}: {str(e)}", fg="red", bold=True), err=True)
                results[action.value] = {"error": str(e)}
                # Continue with next action instead of failing completely
        
        return results
    
    def validate_action_combination(self) -> tuple[bool, Optional[str]]:
        """Validate that selected actions can be executed together.
        
        Returns:
            Tuple of (is_valid, error_message)
        """
        if not self.actions:
            return False, "No actions selected"
        
        # Check for exclusive actions
        exclusive_found = [a for a in self.actions if a in self.EXCLUSIVE_ACTIONS]
        if len(exclusive_found) > 0 and len(self.actions) > len(exclusive_found):
            return False, (
                f"Cannot combine {exclusive_found[0].value} with other actions. "
                f"Selected: {[a.value for a in self.actions]}"
            )
        
        return True, None
