from typing import Optional
import logging
import slicer
import qt

LAST_NOTIFICATION = None # globally tracks the last notification that was shown

class Notification(qt.QWidget):
    """
    Notification widget that can be shown temporarily on top of its parent.
    """
    def __init__(self, message:str, parent=None, previous_notification:"Optional[Notification]"=None):
        """
        If parent is not provided then it will be the Slicer layout container that contains the slice and 3D views.

        Another Notification object can optionally be provided as `previous_notification`.
        If one is provided then this notification will be positioned below `previous_notification`, rather than 
        being at the top-middle-ish of the parent widget.
        """
        if parent is None:
            parent = slicer.app.layoutManager().parent()

        super().__init__(parent)
        
        self.previous_notification = previous_notification

        self.setWindowFlags(
            qt.Qt.Tool | # tooltip like behavior
            qt.Qt.FramelessWindowHint |
            qt.Qt.WindowStaysOnTopHint
        )
        self.setAttribute(qt.Qt.WA_ShowWithoutActivating) # prevents widget from grabbing focus
        self.setAttribute(qt.Qt.WA_TranslucentBackground)

        self.setStyleSheet("""
            QWidget {
                background-color: rgba(30, 30, 30, 220);
                color: white;
                border-radius: 5px;
            }
        """)

        layout = qt.QVBoxLayout(self)
        self.label = qt.QLabel(message, self)
        self.label.setContentsMargins(15, 10, 15, 10) # padding
        layout.addWidget(self.label)
        
        self.adjustSize() # adjust size to fit the text

    def move_to_below_previous_notification(self):
        prev_pos = self.previous_notification.pos
        target_x = prev_pos.x() + (self.previous_notification.width - self.width) // 2
        target_y = prev_pos.y() + self.previous_notification.height
        self.move(target_x, target_y)

    def move_to_top_center_of_parent(self):
        parent_widget = self.parent()
        parent_global_pos = parent_widget.mapToGlobal(qt.QPoint(0, 0))
        target_x = parent_global_pos.x() + (parent_widget.width - self.width) // 2
        target_y = parent_global_pos.y() + 50 
        self.move(target_x, target_y)

    def show_temporarily(self, duration_ms=3000):

        if self.previous_notification is not None and self.previous_notification.isVisible():
            self.move_to_below_previous_notification()
        else:
            self.move_to_top_center_of_parent()

        self.show()
        qt.QTimer.singleShot(duration_ms, self.close)

def notify(message:str) -> None:
    """Submit push notification, a temporarily but somewhat prominently displayed piece of text that also gets logged"""
    logging.info(message)

    global LAST_NOTIFICATION

    previous_notification_to_use = None
    if LAST_NOTIFICATION is not None and LAST_NOTIFICATION.isVisible():
        previous_notification_to_use = LAST_NOTIFICATION
    
    notification = Notification(message=message, previous_notification=previous_notification_to_use)
    notification.show_temporarily()
    LAST_NOTIFICATION = notification