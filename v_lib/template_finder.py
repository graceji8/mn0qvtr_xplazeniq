"""
Main template finder class that combines all functionality
"""

import time
from typing import Tuple, Optional, List
from template_finder_core import ScreenCapture, TemplateLoader, TemplateMatcher, ResultSaver
from mouse_controller import MouseController


class ScreenTemplateFinder:
    """Main class for finding and interacting with templates on screen"""
    
    def __init__(self, confidence_threshold: float = 0.8):
        self.confidence_threshold = confidence_threshold
        self.matcher = TemplateMatcher(confidence_threshold)
        self.mouse = MouseController()
        self.screen_capture = ScreenCapture()
        self.template_loader = TemplateLoader()
        self.result_saver = ResultSaver()
    
    def find_template_in_screen(self, template_path: str, 
                               bbox: Optional[Tuple[int, int, int, int]] = None,
                               search_area: Optional[Tuple[int, int, int, int]] = None,
            save_result: bool = False,
            result_path: str = "template_match_result.png",
            threshold: Optional[float] = None,
            scales: Optional[List[float]] = None) -> List[Tuple[int, int, int, int, float, float]]:
        """
        Complete workflow: capture screen and find template
        """
        template = self.template_loader.load(template_path)
        if template is None:
            return []
        
        screenshot, offset = self.screen_capture.capture(bbox)
        if screenshot is None:
            return []
        
        adjusted_search_area = search_area
        if search_area and any(v > 1.0 for v in search_area):
            adjusted_search_area = (
                search_area[0] - offset[0],
                search_area[1] - offset[1],
                search_area[2] - offset[0],
                search_area[3] - offset[1]
            )
        
        matches = self.matcher.find_matches(screenshot, template, search_area=adjusted_search_area, threshold=threshold, scales=scales)
        
        if matches:
            adjusted_matches = []
            for x, y, w, h, confidence, mean_diff in matches:
                adjusted_matches.append((x + offset[0], y + offset[1], w, h, confidence, mean_diff))
            matches = adjusted_matches
        
        if save_result and matches:
            self.result_saver.save_annotated(screenshot, matches, result_path)
        
        return matches
    
    def click_template(self, template_path: str, 
                       bbox: Optional[Tuple[float, float, float, float]] = None,
                       search_area: Optional[Tuple[float, float, float, float]] = None,
                       click_offset: Tuple[int, int] = (0, 0),
                       button: str = 'left',
                       save_result: bool = False,
                       return_to_original: bool = False,
                       return_to_top: bool = False,
                       return_position: Optional[Tuple[int, int]] = None,
                       threshold: Optional[float] = None,
                       scales: Optional[List[float]] = None) -> bool:
        """Find template and click on it"""
        if return_to_original:
            self.mouse.store_position()
        
        matches = self.find_template_in_screen(template_path, bbox, search_area, save_result=save_result, threshold=threshold, scales=scales)
        
        if not matches:
            print("Template not found for clicking")
            return False
        
        x, y, w, h, confidence, mean_diff = matches[0]
        click_x = x + w // 2 + click_offset[0]
        click_y = y + h // 2 + click_offset[1]
        
        print(f"Template found at ({x}, {y}) with confidence {confidence:.2f} (Color Diff: {mean_diff:.2f})")
        print(f"Clicking at position: ({click_x}, {click_y})")
        
        success = self.mouse.click(click_x, click_y, button)
        
        if success:
            if return_position:
                self.mouse.return_to_position(return_position)
            elif return_to_top:
                self.mouse.return_to_top()
            elif return_to_original:
                self.mouse.return_to_position()
        
        return success
    
    def mouseover_template(self, template_path: str, 
                           bbox: Optional[Tuple[float, float, float, float]] = None,
                           search_area: Optional[Tuple[float, float, float, float]] = None,
                           hover_offset: Tuple[int, int] = (0, 0),
                           hover_delay: float = 0.5,
                           save_result: bool = False,
                           return_to_original: bool = False,
                           return_to_top: bool = False,
                           return_position: Optional[Tuple[int, int]] = None,
                           threshold: Optional[float] = None,
                           scales: Optional[List[float]] = None) -> bool:
        """Find template and hover mouse over it"""
        if return_to_original:
            self.mouse.store_position()
        
        matches = self.find_template_in_screen(template_path, bbox, search_area, save_result=save_result, threshold=threshold, scales=scales)
        
        if not matches:
            print("Template not found for mouseover")
            return False
        
        x, y, w, h, confidence, mean_diff = matches[0]
        hover_x = x + w // 2 + hover_offset[0]
        hover_y = y + h // 2 + hover_offset[1]
        
        print(f"Template found at ({x}, {y}) with confidence {confidence:.2f} (Color Diff: {mean_diff:.2f})")
        print(f"Hovering at position: ({hover_x}, {hover_y})")
        
        success = self.mouse.hover(hover_x, hover_y, hover_delay)
        
        if success:
            if return_position:
                self.mouse.return_to_position(return_position)
            elif return_to_top:
                self.mouse.return_to_top()
            elif return_to_original:
                self.mouse.return_to_position()
        
        return success
    
    def find_and_click_all_templates(self, template_path: str,
                                    bbox: Optional[Tuple[float, float, float, float]] = None,
                                    search_area: Optional[Tuple[float, float, float, float]] = None,
                                    click_offset: Tuple[int, int] = (0, 0),
                                    button: str = 'left',
                                    delay_between_clicks: float = 0.5,
                                    return_to_original: bool = False,
                                    return_to_top: bool = False,
                                    return_position: Optional[Tuple[int, int]] = None,
                                    threshold: Optional[float] = None,
                                    scales: Optional[List[float]] = None) -> int:
        """Find all instances of template and click on each one"""
        if return_to_original:
            self.mouse.store_position()
        
        matches = self.find_template_in_screen(template_path, bbox, search_area, threshold=threshold, scales=scales)
        
        if not matches:
            print("No templates found for clicking")
            return 0
        
        clicked_count = 0
        
        for i, match in enumerate(matches):
            x, y, w, h, confidence, mean_diff = match
            click_x = x + w // 2 + click_offset[0]
            click_y = y + h // 2 + click_offset[1]
            
            print(f"Clicking template {i+1}/{len(matches)} at ({click_x}, {click_y}) - conf: {confidence:.2f}, diff: {mean_diff:.2f}")
            
            if self.mouse.click(click_x, click_y, button):
                clicked_count += 1
                
                if i < len(matches) - 1 and delay_between_clicks > 0:
                    time.sleep(delay_between_clicks)
        
        if clicked_count > 0:
            if return_position:
                self.mouse.return_to_position(return_position)
            elif return_to_top:
                self.mouse.return_to_top()
            elif return_to_original:
                self.mouse.return_to_position()
        
        print(f"Successfully clicked {clicked_count}/{len(matches)} templates")
        return clicked_count
    
    def wait_for_template(self, template_path: str, 
                          timeout: float = 30.0,
                          check_interval: float = 1.0,
                          bbox: Optional[Tuple[float, float, float, float]] = None,
                          search_area: Optional[Tuple[float, float, float, float]] = None,
                          threshold: Optional[float] = None,
                          scales: Optional[List[float]] = None) -> Optional[Tuple[int, int, int, int, float, float]]:
        """Wait for template to appear on screen"""
        start_time = time.time()
        
        print(f"Waiting for template '{template_path}' (timeout: {timeout}s)...")
        
        while time.time() - start_time < timeout:
            matches = self.find_template_in_screen(template_path, bbox, search_area, threshold=threshold, scales=scales)
            
            if matches:
                print(f"Template found! Confidence: {matches[0][4]:.2f}, Color Diff: {matches[0][5]:.2f}")
                return matches[0]
            
            time.sleep(check_interval)
        
        print(f"Template not found within {timeout} seconds")
        return None
    
    def wait_and_click_template(self, template_path: str,
                                timeout: float = 30.0,
                                check_interval: float = 1.0,
                                bbox: Optional[Tuple[float, float, float, float]] = None,
                                search_area: Optional[Tuple[float, float, float, float]] = None,
                                click_offset: Tuple[int, int] = (0, 0),
                                button: str = 'left',
                                return_to_original: bool = False,
                                return_to_top: bool = False,
                                return_position: Optional[Tuple[int, int]] = None,
                                threshold: Optional[float] = None,
                                times: int = 1,
                                scales: Optional[List[float]] = None) -> bool:
        """Wait for template to appear and then click it."""
        if return_to_original:
            self.mouse.store_position()
        
        for attempt in range(times):
            match = self.wait_for_template(template_path, timeout, check_interval, bbox, search_area, threshold=threshold, scales=scales)
            if match is not None:
                x, y, w, h, confidence, mean_diff = match
                click_x = x + w // 2 + click_offset[0]
                click_y = y + h // 2 + click_offset[1]
                if self.mouse.click(click_x, click_y, button):
                    if return_position:
                        self.mouse.return_to_position(return_position)
                    elif return_to_top:
                        self.mouse.return_to_top()
                    elif return_to_original:
                        self.mouse.return_to_position()
                    return True
            if attempt < times - 1:
                time.sleep(1.0)
        return False
    
    def check_template_exists(self, template_path: str, 
                             bbox: Optional[Tuple[float, float, float, float]] = None,
                             search_area: Optional[Tuple[float, float, float, float]] = None,
                             verbose: bool = True,
                             threshold: Optional[float] = None,
                             max_mean_diff: Optional[float] = None,
                             scales: Optional[List[float]] = None) -> bool:
        """Simple check if template exists on screen"""
        matches = self.find_template_in_screen(template_path, bbox, search_area, threshold=threshold, scales=scales)
        if max_mean_diff is not None:
            matches = [m for m in matches if m[5] <= max_mean_diff]
        exists = len(matches) > 0
        if verbose:
            if exists:
                print(f"✓ Template found")
            else:
                print(f"✗ Template not found")
        return exists

    def position_template_exists(self, template_path: str, 
                                 bbox: Optional[Tuple[float, float, float, float]] = None,
                                 search_area: Optional[Tuple[float, float, float, float]] = None,
                                 verbose: bool = True,
                                 threshold: Optional[float] = None,
                                 max_mean_diff: Optional[float] = None,
                                 scales: Optional[List[float]] = None) -> Optional[Tuple[int, int]]:
        """Check if template exists and return its center position (x, y)."""
        matches = self.find_template_in_screen(template_path, bbox, search_area, threshold=threshold, scales=scales)
        if max_mean_diff is not None:
            matches = [m for m in matches if m[5] <= max_mean_diff]
        if not matches:
            return None
        best_match = max(matches, key=lambda x: x[4])
        x, y, w, h, conf, diff = best_match
        return (x + w // 2, y + h // 2)
