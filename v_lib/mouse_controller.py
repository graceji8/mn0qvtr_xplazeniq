"""
Mouse control operations
"""

import pyautogui
import time
from typing import Tuple, Optional


class MouseController:
    """Handles mouse movement and clicking operations"""
    
    def __init__(self):
        self.original_position = None
    
    def get_position(self) -> Optional[Tuple[int, int]]:
        """Get current mouse position"""
        try:
            x, y = pyautogui.position()
            return (x, y)
        except Exception:
            return None
    
    def store_position(self):
        """Store current mouse position for later restoration"""
        self.original_position = self.get_position()
    
    def move_to(self, x: int, y: int) -> bool:
        """Move mouse to specified position"""
        try:
            pyautogui.moveTo(x, y)
            return True
        except Exception:
            return False
    
    def return_to_position(self, position: Optional[Tuple[int, int]] = None) -> bool:
        """
        Return mouse to specified position or stored original position
        """
        if position is None:
            position = self.original_position
        if position is None:
            return False
        x, y = position
        try:
            pyautogui.moveTo(x, y)
            return True
        except Exception:
            return False
    
    def return_to_top(self, x: int = 0) -> bool:
        """Return mouse to top of screen"""
        return self.return_to_position((x, 0))
    
    def click(self, x: int, y: int, button: str = 'left', delay: float = 0.5) -> bool:
        """
        Click at specified coordinates with 'Deep Click' reliability
        """
        try:
            pyautogui.moveTo(x, y, duration=0.5)
            time.sleep(3.0)
            pyautogui.mouseDown(x, y, button=button)
            time.sleep(0.2)
            pyautogui.mouseUp(x, y, button=button)
            if delay > 0:
                time.sleep(delay)
            return True
        except Exception:
            return False
    
    def hover(self, x: int, y: int, delay: float = 0.5) -> bool:
        """
        Move mouse to specified coordinates and hover
        """
        try:
            pyautogui.moveTo(x, y)
            if delay > 0:
                time.sleep(delay)
            return True
        except Exception:
            return False
