"""
Core template finding functionality
"""

import cv2
import numpy as np
import pyautogui
from typing import Tuple, Optional, List
import os
import ctypes
import mss

# Try to make the process DPI aware
try:
    ctypes.windll.shcore.SetProcessDpiAwareness(1)
except Exception:
    try:
        ctypes.windll.user32.SetProcessDPIAware()
    except Exception:
        pass

# PyAutoGUI safety settings
pyautogui.FAILSAFE = True
pyautogui.PAUSE = 0.1


class ScreenCapture:
    """Handles screen capture operations"""
    
    @staticmethod
    def capture(bbox: Optional[Tuple[float, float, float, float]] = None) -> Tuple[Optional[np.ndarray], Tuple[int, int]]:
        """
        Capture screenshot using mss
        """
        try:
            with mss.mss() as sct:
                primary_monitor = sct.monitors[1] if len(sct.monitors) > 1 else sct.monitors[0]
                screen_w = primary_monitor["width"]
                screen_h = primary_monitor["height"]

                if bbox:
                    left, top, right, bottom = bbox
                    if all(0 <= v <= 1.0 for v in [left, top, right, bottom]):
                        left = int(left * screen_w)
                        top = int(top * screen_h)
                        right = int(right * screen_w)
                        bottom = int(bottom * screen_h)
                    else:
                        left, top, right, bottom = int(left), int(top), int(right), int(bottom)
                    monitor = {"top": top, "left": left, "width": right - left, "height": bottom - top}
                    offset = (left, top)
                else:
                    monitor = sct.monitors[0]
                    offset = (monitor['left'], monitor['top'])
                
                sct_img = sct.grab(monitor)
                screenshot_np = np.array(sct_img)
                screenshot_bgr = cv2.cvtColor(screenshot_np, cv2.COLOR_BGRA2BGR)
                
            return screenshot_bgr, offset
        except Exception as e:
            print(f"Error capturing screen: {e}")
            return None, (0, 0)


class TemplateLoader:
    """Handles template image loading"""
    
    @staticmethod
    def load(template_path: str) -> Optional[np.ndarray]:
        """
        Load template image from file
        """
        try:
            if not os.path.exists(template_path):
                return None
            template = cv2.imread(template_path, cv2.IMREAD_COLOR)
            return template
        except Exception:
            return None


class TemplateMatcher:
    """Handles template matching operations"""
    
    def __init__(self, confidence_threshold: float = 0.8):
        self.confidence_threshold = confidence_threshold
    
    def find_matches(self, screenshot: np.ndarray, template: np.ndarray, 
                     search_area: Optional[Tuple[float, float, float, float]] = None,
                     threshold: Optional[float] = None,
                     scales: Optional[List[float]] = None) -> List[Tuple[int, int, int, int, float, float]]:
        """
        Find template in screenshot using OpenCV template matching
        """
        try:
            current_threshold = threshold if threshold is not None else self.confidence_threshold
            offset_x = 0
            offset_y = 0
            if search_area:
                left, top, right, bottom = search_area
                if all(0 <= v <= 1.0 for v in [left, top, right, bottom]):
                    left = int(left * screenshot.shape[1])
                    top = int(top * screenshot.shape[0])
                    right = int(right * screenshot.shape[1])
                    bottom = int(bottom * screenshot.shape[0])
                else:
                    left, top, right, bottom = int(left), int(top), int(right), int(bottom)
                screenshot = screenshot[top:bottom, left:right]
                offset_x = left
                offset_y = top
            
            all_matches = []
            search_scales = scales if scales is not None else [1.0]

            for scale in search_scales:
                if scale != 1.0:
                    scaled_template = cv2.resize(template, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA)
                else:
                    scaled_template = template
                
                t_h, t_w = scaled_template.shape[:2]
                if t_h > screenshot.shape[0] or t_w > screenshot.shape[1]:
                    continue

                result = cv2.matchTemplate(screenshot, scaled_template, cv2.TM_CCOEFF_NORMED)
                locations = np.where(result >= current_threshold)
                
                for pt in zip(*locations[::-1]):
                    x, y = pt
                    confidence = float(result[y, x])
                    matched_region = screenshot[y:y+t_h, x:x+t_w]
                    diff = cv2.absdiff(matched_region.astype(np.float32), scaled_template.astype(np.float32))
                    mean_diff = float(np.mean(diff))
                    all_matches.append((x + offset_x, y + offset_y, t_w, t_h, confidence, mean_diff))
            
            if all_matches:
                all_matches = self._remove_duplicates(all_matches)
            
            return all_matches
        except Exception:
            return []
    
    def _remove_duplicates(self, matches: List[Tuple[int, int, int, int, float, float]], 
                           overlap_threshold: float = 0.3) -> List[Tuple[int, int, int, int, float, float]]:
        """Remove duplicate/overlapping matches using Non-Maximum Suppression"""
        if not matches:
            return []
        matches = sorted(matches, key=lambda x: x[4], reverse=True)
        filtered_matches = []
        for match in matches:
            x1, y1, w1, h1, conf, md1 = match
            is_duplicate = False
            for accepted_match in filtered_matches:
                x2, y2, w2, h2, _, _ = accepted_match
                intersection_x1 = max(x1, x2)
                intersection_y1 = max(y1, y2)
                intersection_x2 = min(x1 + w1, x2 + w2)
                intersection_y2 = min(y1 + h1, y2 + h2)
                if intersection_x2 > intersection_x1 and intersection_y2 > intersection_y1:
                    intersection_area = (intersection_x2 - intersection_x1) * (intersection_y2 - intersection_y1)
                    area1 = w1 * h1
                    if (intersection_area / area1) > overlap_threshold:
                        is_duplicate = True
                        break
            if not is_duplicate:
                filtered_matches.append(match)
        return filtered_matches


class ResultSaver:
    """Handles saving annotated results"""
    
    @staticmethod
    def save_annotated(screenshot: np.ndarray, 
                      matches: List[Tuple[int, int, int, int, float, float]], 
                      output_path: str):
        try:
            annotated = screenshot.copy()
            for i, match in enumerate(matches):
                x, y, w, h, conf, _ = match
                cv2.rectangle(annotated, (x, y), (x + w, y + h), (0, 255, 0), 2)
            cv2.imwrite(output_path, annotated)
        except Exception:
            pass
