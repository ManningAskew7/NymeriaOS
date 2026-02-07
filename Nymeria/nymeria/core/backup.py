"""Backup manager for Nymeria self-modification system."""

import logging
import shutil
from datetime import datetime
from pathlib import Path
from typing import List, Optional

logger = logging.getLogger(__name__)


class BackupManager:
    """
    Manages file backups for safe self-modification.

    Creates timestamped backups before any file modification,
    supports rollback, and cleans up old backups automatically.
    """

    MAX_BACKUPS_PER_FILE = 10

    def __init__(self, backup_dir: Path, project_root: Path):
        """
        Initialize the backup manager.

        Args:
            backup_dir: Directory to store backups (e.g., data/backups)
            project_root: Root directory of the project (for relative paths)
        """
        self.backup_dir = backup_dir
        self.project_root = project_root
        self.backup_dir.mkdir(parents=True, exist_ok=True)
        logger.info(f"BackupManager initialized with directory: {self.backup_dir}")

    def _get_backup_subdir(self, file_path: Path) -> Path:
        """Get the backup subdirectory for a file (mirrors source structure)."""
        try:
            relative = file_path.relative_to(self.project_root)
            return self.backup_dir / relative.parent
        except ValueError:
            # File is outside project root, use absolute path hash
            return self.backup_dir / "_external" / str(hash(str(file_path)))[-8:]

    def _generate_backup_name(self, file_path: Path) -> str:
        """Generate a timestamped backup filename."""
        timestamp = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
        return f"{file_path.stem}_{timestamp}{file_path.suffix}"

    def create_backup(self, file_path: Path) -> Optional[Path]:
        """
        Create a backup of a file before modification.

        Args:
            file_path: Path to the file to backup

        Returns:
            Path to the backup file, or None if file doesn't exist
        """
        file_path = Path(file_path).resolve()

        if not file_path.exists():
            logger.warning(f"Cannot backup non-existent file: {file_path}")
            return None

        if not file_path.is_file():
            logger.warning(f"Cannot backup non-file: {file_path}")
            return None

        # Create backup directory structure
        backup_subdir = self._get_backup_subdir(file_path)
        backup_subdir.mkdir(parents=True, exist_ok=True)

        # Generate backup path
        backup_name = self._generate_backup_name(file_path)
        backup_path = backup_subdir / backup_name

        # Copy file to backup
        try:
            shutil.copy2(file_path, backup_path)
            logger.info(f"Created backup: {file_path} -> {backup_path}")

            # Clean up old backups
            self._cleanup_old_backups(file_path)

            return backup_path

        except Exception as e:
            logger.error(f"Failed to create backup for {file_path}: {e}")
            return None

    def restore_backup(self, file_path: Path, backup_path: Optional[Path] = None) -> bool:
        """
        Restore a file from backup.

        Args:
            file_path: Original file path to restore to
            backup_path: Specific backup to restore (uses latest if None)

        Returns:
            True if restored successfully
        """
        file_path = Path(file_path).resolve()

        if backup_path is None:
            backup_path = self.get_latest_backup(file_path)
            if backup_path is None:
                logger.error(f"No backup found for: {file_path}")
                return False

        backup_path = Path(backup_path).resolve()

        if not backup_path.exists():
            logger.error(f"Backup file not found: {backup_path}")
            return False

        try:
            # Ensure parent directory exists
            file_path.parent.mkdir(parents=True, exist_ok=True)

            shutil.copy2(backup_path, file_path)
            logger.info(f"Restored from backup: {backup_path} -> {file_path}")
            return True

        except Exception as e:
            logger.error(f"Failed to restore backup {backup_path}: {e}")
            return False

    def get_latest_backup(self, file_path: Path) -> Optional[Path]:
        """
        Get the most recent backup for a file.

        Args:
            file_path: Original file path

        Returns:
            Path to the latest backup, or None if no backups exist
        """
        file_path = Path(file_path).resolve()
        backup_subdir = self._get_backup_subdir(file_path)

        if not backup_subdir.exists():
            return None

        # Find all backups for this file (matching stem pattern)
        pattern = f"{file_path.stem}_*{file_path.suffix}"
        backups = list(backup_subdir.glob(pattern))

        if not backups:
            return None

        # Sort by modification time (newest first)
        backups.sort(key=lambda p: p.stat().st_mtime, reverse=True)
        return backups[0]

    def list_backups(self, file_path: Path) -> List[Path]:
        """
        List all backups for a file.

        Args:
            file_path: Original file path

        Returns:
            List of backup paths, sorted newest first
        """
        file_path = Path(file_path).resolve()
        backup_subdir = self._get_backup_subdir(file_path)

        if not backup_subdir.exists():
            return []

        pattern = f"{file_path.stem}_*{file_path.suffix}"
        backups = list(backup_subdir.glob(pattern))

        # Sort by modification time (newest first)
        backups.sort(key=lambda p: p.stat().st_mtime, reverse=True)
        return backups

    def _cleanup_old_backups(self, file_path: Path) -> int:
        """
        Remove old backups exceeding the limit.

        Args:
            file_path: Original file path

        Returns:
            Number of backups removed
        """
        backups = self.list_backups(file_path)

        if len(backups) <= self.MAX_BACKUPS_PER_FILE:
            return 0

        # Remove oldest backups
        removed = 0
        for backup in backups[self.MAX_BACKUPS_PER_FILE:]:
            try:
                backup.unlink()
                removed += 1
                logger.debug(f"Cleaned up old backup: {backup}")
            except Exception as e:
                logger.warning(f"Failed to remove old backup {backup}: {e}")

        return removed

    def delete_all_backups(self, file_path: Path) -> int:
        """
        Delete all backups for a file.

        Args:
            file_path: Original file path

        Returns:
            Number of backups deleted
        """
        backups = self.list_backups(file_path)
        deleted = 0

        for backup in backups:
            try:
                backup.unlink()
                deleted += 1
            except Exception as e:
                logger.warning(f"Failed to delete backup {backup}: {e}")

        return deleted

    def get_backup_info(self) -> dict:
        """
        Get summary information about all backups.

        Returns:
            Dict with backup statistics
        """
        total_files = 0
        total_size = 0

        for path in self.backup_dir.rglob("*"):
            if path.is_file():
                total_files += 1
                total_size += path.stat().st_size

        return {
            "backup_dir": str(self.backup_dir),
            "total_files": total_files,
            "total_size_bytes": total_size,
            "total_size_mb": round(total_size / (1024 * 1024), 2),
        }
