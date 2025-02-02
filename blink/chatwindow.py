
import base64
import locale
import os
import re

from PyQt5 import uic
from PyQt5.QtCore import Qt, QBuffer, QEasingCurve, QEvent, QPoint, QPointF, QPropertyAnimation, QRect, QRectF, QSettings, QSize, QSizeF, QTimer, QUrl, pyqtSignal
from PyQt5.QtGui import QBrush, QColor, QIcon, QImageReader, QKeyEvent, QLinearGradient, QPainter, QPalette, QPen, QPixmap, QPolygonF, QTextCharFormat, QTextCursor, QTextDocument
from PyQt5.QtGui import QDesktopServices
from PyQt5.QtWebKit import QWebSettings
from PyQt5.QtWebKitWidgets import QWebPage, QWebView
from PyQt5.QtWidgets import QApplication, QAction, QLabel, QListView, QMenu, QStyle, QStyleOption, QStylePainter, QTextEdit, QToolButton

from abc import ABCMeta, abstractmethod
from application.notification import IObserver, NotificationCenter, ObserverWeakrefProxy
from application.python import Null, limit
from application.python.descriptor import WriteOnceAttribute
from application.python.types import MarkerType
from application.system import makedirs
from collections import MutableSet, deque
from datetime import datetime, timedelta
from itertools import count
from lxml import etree, html
from lxml.html.clean import autolink
from weakref import proxy
from zope.interface import implementer

from sipsimple.account import AccountManager
from sipsimple.application import SIPApplication
from sipsimple.audio import WavePlayer
from sipsimple.configuration.settings import SIPSimpleSettings
from sipsimple.streams.msrp.chat import OTRState
from sipsimple.threading import run_in_thread

from blink.configuration.datatypes import FileURL, GraphTimeScale
from blink.configuration.settings import BlinkSettings
from blink.contacts import URIUtils
from blink.resources import IconManager, Resources
from blink.sessions import ChatSessionModel, ChatSessionListView, SessionManager, StreamDescription
from blink.util import run_in_gui_thread
from blink.widgets.color import ColorHelperMixin
from blink.widgets.graph import Graph
from blink.widgets.otr import OTRWidget
from blink.widgets.util import ContextMenuActions, QtDynamicProperty
from blink.widgets.video import VideoSurface
from blink.widgets.zrtp import ZRTPWidget


__all__ = ['ChatWindow']


class Container(object): pass


# Chat style classes
#

class ChatStyleError(Exception): pass


class ChatHtmlTemplates(object):
    def __init__(self, style_path):
        try:
            self.message = open(os.path.join(style_path, 'html/message.html')).read()
            self.message_continuation = open(os.path.join(style_path, 'html/message_continuation.html')).read()
            self.notification = open(os.path.join(style_path, 'html/notification.html')).read()
        except (OSError, IOError):
            raise ChatStyleError("missing or unreadable chat message html template files in %s" % os.path.join(style_path, 'html'))


class ChatMessageStyle(object):
    def __init__(self, name):
        self.name = name
        self.path = Resources.get('chat/styles/%s' % name)
        try:
            xml_tree = etree.parse(os.path.join(self.path, 'style.xml'), parser=etree.XMLParser(resolve_entities=False))
        except (etree.ParseError, OSError, IOError):
            self.info = {}
        else:
            self.info = dict((element.tag, element.text) for element in xml_tree.getroot())
        try:
            self.variants = tuple(sorted(name[:-len('.style')] for name in os.listdir(self.path) if name.endswith('.style')))
        except (OSError, IOError):
            self.variants = ()
        if not self.variants:
            raise ChatStyleError("chat style %s contains no variants" % name)
        self.html = ChatHtmlTemplates(self.path)

    @property
    def default_variant(self):
        default_variant = self.info.get('default_variant')
        return default_variant if default_variant in self.variants else self.variants[0]

    @property
    def font_family(self):
        return self.info.get('font_family', 'sans-serif')

    @property
    def font_size(self):
        try:
            return int(self.info['font_size'])
        except (KeyError, ValueError):
            return 11


# Chat content classes
#

class Link(object):
    __slots__ = 'prev', 'next', '__next__', 'key', '__weakref__'


class OrderedSet(MutableSet):
    def __init__(self, iterable=None):
        self.__hardroot = Link()  # sentinel node for doubly linked list
        self.__root = root = proxy(self.__hardroot)
        root.prev = root.__next__ = root
        self.__map = {}
        if iterable is not None:
            self |= iterable

    def __len__(self):
        return len(self.__map)

    def __contains__(self, key):
        return key in self.__map

    def __iter__(self):
        root = self.__root
        curr = root.__next__
        while curr is not root:
            yield curr.key
            curr = curr.__next__

    def __reversed__(self):
        root = self.__root
        curr = root.prev
        while curr is not root:
            yield curr.key
            curr = curr.prev

    def __repr__(self):
        return '%s(%r)' % (self.__class__.__name__, list(self))

    def add(self, key):
        if key not in self.__map:
            self.__map[key] = link = Link()
            root = self.__root
            last = root.prev
            link.prev, link.next, link.key = last, root, key
            last.next = link
            root.prev = proxy(link)

    def discard(self, key):
        if key in self.__map:
            link = self.__map.pop(key)
            link_prev = link.prev
            link_next = link.__next__
            link_prev.next = link_next
            link_next.prev = link_prev

    def clear(self):
        root = self.__root
        root.prev = root.__next__ = root
        self.__map.clear()


class ChatContentBooleanOption(object):
    """Adds/removes name from css classes based on option being True/False"""

    def __init__(self, name):
        self.name = name

    def __get__(self, instance, owner):
        if instance is None:
            return self
        return self.name in instance.__cssclasses__

    def __set__(self, obj, value):
        if value:
            obj.__cssclasses__.add(self.name)
        else:
            obj.__cssclasses__.discard(self.name)

    def __delete__(self, obj):
        raise AttributeError('attribute cannot be deleted')


class AnyValue(metaclass=MarkerType): pass


class ChatContentStringAttribute(object):
    """A string attribute that is also added as a css class"""

    def __init__(self, name, allowed_values=AnyValue):
        self.name = name
        self.allowed_values = allowed_values

    def __get__(self, instance, owner):
        if instance is None:
            return self
        try:
            return instance.__dict__[self.name]
        except KeyError:
            raise AttributeError("'{}' attribute is not set".format(self.name))

    def __set__(self, obj, value):
        if self.allowed_values is not AnyValue and value not in self.allowed_values:
            raise ValueError("invalid value for '{}': '{}'".format(self.name, value))
        old_value = obj.__dict__.get(self.name, None)
        obj.__cssclasses__.discard(old_value)
        if value is not None:
            obj.__cssclasses__.add(value)
        obj.__dict__[self.name] = value

    def __delete__(self, obj):
        raise AttributeError('attribute cannot be deleted')


class ChatContent(object, metaclass=ABCMeta):
    __cssclasses__ = ()

    continuation_interval = timedelta(0, 5*60)  # 5 minutes

    history = ChatContentBooleanOption('history')
    focus = ChatContentBooleanOption('focus')
    consecutive = ChatContentBooleanOption('consecutive')
    mention = ChatContentBooleanOption('mention')  # keep it here? or keep it at all? -Dan

    def __init__(self, message, history=False, focus=False):
        self.__cssclasses__ = OrderedSet(self.__class__.__cssclasses__)
        self.message = message
        self.history = history
        self.focus = focus
        self.timestamp = datetime.now()

    @property
    def css_classes(self):
        return ' '.join(self.__cssclasses__)

    @property
    def date(self):
        language, encoding = locale.getlocale(locale.LC_TIME)
        return self.timestamp.strftime('%d %b %Y')

    @property
    def time(self):
        language, encoding = locale.getlocale(locale.LC_TIME)
        return self.timestamp.strftime('%H:%M')

    @property
    def text_direction(self):
        try:
            return self.__dict__['text_direction']
        except KeyError:
            document = QTextDocument()
            document.setHtml(self.message)
            return self.__dict__.setdefault('text_direction', 'rtl' if document.firstBlock().textDirection() == Qt.RightToLeft else 'ltr')

    def add_css_class(self, name):
        self.__cssclasses__.add(name)

    def is_related_to(self, other):
        return type(self) is type(other) and self.history == other.history and timedelta(0) <= self.timestamp - other.timestamp <= self.continuation_interval

    @abstractmethod
    def to_html(self, style, **kw):
        raise NotImplementedError


class ChatNotification(ChatContent):
    __cssclasses__ = ('event',)

    def to_html(self, style, **kw):
        return style.html.notification.format(message=self, **kw)


class ChatEvent(ChatNotification):
    __cssclasses__ = ('event',)


class ChatStatus(ChatNotification):
    __cssclasses__ = ('status',)


class ChatMessage(ChatContent):
    __cssclasses__ = ('message',)

    direction = ChatContentStringAttribute('direction', allowed_values=('incoming', 'outgoing'))
    autoreply = ChatContentBooleanOption('autoreply')

    def __init__(self, message, sender, direction, history=False, focus=False):
        super(ChatMessage, self).__init__(message, history, focus)
        self.sender = sender
        self.direction = direction

    def is_related_to(self, other):
        return super(ChatMessage, self).is_related_to(other) and self.sender == other.sender and self.direction == other.direction

    def to_html(self, style, **kw):
        if self.consecutive:
            return style.html.message_continuation.format(message=self, **kw)
        else:
            return style.html.message.format(message=self, **kw)


class ChatSender(object):
    __colors__ = ["aqua", "aquamarine", "blue", "blueviolet", "brown", "burlywood", "cadetblue", "chartreuse", "chocolate", "coral", "cornflowerblue", "crimson", "cyan", "darkblue", "darkcyan",
                  "darkgoldenrod", "darkgreen", "darkgrey", "darkkhaki", "darkmagenta", "darkolivegreen", "darkorange", "darkorchid", "darkred", "darksalmon", "darkseagreen", "darkslateblue",
                  "darkslategrey", "darkturquoise", "darkviolet", "deeppink", "deepskyblue", "dimgrey", "dodgerblue", "firebrick", "forestgreen", "fuchsia", "gold", "goldenrod", "green",
                  "greenyellow", "grey", "hotpink", "indianred", "indigo", "lawngreen", "lightblue", "lightcoral", "lightgreen", "lightgrey", "lightpink", "lightsalmon", "lightseagreen",
                  "lightskyblue", "lightslategrey", "lightsteelblue", "lime", "limegreen", "magenta", "maroon", "mediumaquamarine", "mediumblue", "mediumorchid", "mediumpurple", "mediumseagreen",
                  "mediumslateblue", "mediumspringgreen", "mediumturquoise", "mediumvioletred", "midnightblue", "navy", "olive", "olivedrab", "orange", "orangered", "orchid", "palegreen",
                  "paleturquoise", "palevioletred", "peru", "pink", "plum", "powderblue", "purple", "red", "rosybrown", "royalblue", "saddlebrown", "salmon", "sandybrown", "seagreen", "sienna",
                  "silver", "skyblue", "slateblue", "slategrey", "springgreen", "steelblue", "tan", "teal", "thistle", "tomato", "turquoise", "violet", "yellowgreen"]

    def __init__(self, name, uri, iconpath):
        self.name = name
        self.uri = uri
        self.iconpath = QUrl.fromLocalFile(iconpath).toString()

    def __eq__(self, other):
        if not isinstance(other, ChatSender):
            return NotImplemented
        return self.name == other.name and self.uri == other.uri

    def __ne__(self, other):
        return not (self == other)

    @property
    def color(self):
        return self.__colors__[hash(self.uri) % len(self.__colors__)]


class ChatWebPage(QWebPage):
    def __init__(self, parent=None):
        super(ChatWebPage, self).__init__(parent)
        self.setLinkDelegationPolicy(QWebPage.DelegateAllLinks)
        self.linkClicked.connect(QDesktopServices.openUrl)
        disable_actions = {QWebPage.OpenLink, QWebPage.OpenLinkInNewWindow, QWebPage.OpenLinkInThisWindow, QWebPage.DownloadLinkToDisk,
                           QWebPage.OpenImageInNewWindow, QWebPage.DownloadImageToDisk, QWebPage.DownloadMediaToDisk,
                           QWebPage.Back, QWebPage.Forward, QWebPage.Stop, QWebPage.Reload}
        for action in (self.action(action) for action in disable_actions):
            action.setVisible(False)

    def acceptNavigationRequest(self, frame, request, navigation_type):  # not sure if needed since we already disabled the corresponding actions. (can they be triggered otherwise?)
        if navigation_type in (QWebPage.NavigationTypeBackOrForward, QWebPage.NavigationTypeReload):
            return False
        return super(ChatWebPage, self).acceptNavigationRequest(frame, request, navigation_type)


class ChatWebView(QWebView):
    sizeChanged = pyqtSignal()

    def __init__(self, parent=None):
        super(ChatWebView, self).__init__(parent)
        palette = self.palette()
        palette.setBrush(QPalette.Base, Qt.transparent)
        self.setPalette(palette)
        self.setPage(ChatWebPage(self))
        self.setAttribute(Qt.WA_OpaquePaintEvent, False)
        self.settings().setAttribute(QWebSettings.DeveloperExtrasEnabled, True)  # temporary for debugging -Dan

    def contextMenuEvent(self, event):
        menu = self.page().createStandardContextMenu()
        if any(action.isVisible() and not action.isSeparator() for action in menu.actions()):
            menu.exec_(event.globalPos())

    def createWindow(self, window_type):
        print("create window of type", window_type)
        return None

    def dragEnterEvent(self, event):
        event.ignore()  # let the parent process DND

    def resizeEvent(self, event):
        super(ChatWebView, self).resizeEvent(event)
        self.sizeChanged.emit()


ui_class, base_class = uic.loadUiType(Resources.get('chat_input_lock.ui'))


class ChatInputLock(base_class, ui_class):
    def __init__(self, parent=None):
        super(ChatInputLock, self).__init__(parent)
        with Resources.directory:
            self.setupUi(self)
        if parent is not None:
            parent.installEventFilter(self)

    def eventFilter(self, watched, event):
        if event.type() == QEvent.Resize:
            self.setGeometry(watched.contentsRect())
        return False

    def dragEnterEvent(self, event):
        event.ignore()  # let the parent process DND

    def paintEvent(self, event):
        option = QStyleOption()
        option.initFrom(self)
        painter = QStylePainter(self)
        painter.setRenderHint(QStylePainter.Antialiasing, True)
        painter.drawPrimitive(QStyle.PE_Widget, option)


class LockType(object, metaclass=MarkerType):
    note_text = None
    button_text = None


class EncryptionLock(LockType):
    note_text = 'Encryption has been terminated by the other party'
    button_text = 'Confirm'


class ChatTextInput(QTextEdit):
    textEntered = pyqtSignal(str)
    lockEngaged = pyqtSignal(object)
    lockReleased = pyqtSignal(object)

    def __init__(self, parent=None):
        super(ChatTextInput, self).__init__(parent)
        self.setTabStopWidth(22)
        self.lock_widget = ChatInputLock(self)
        self.lock_widget.hide()
        self.lock_widget.confirm_button.clicked.connect(self._SH_LockWidgetConfirmButtonClicked)
        self.document().documentLayout().documentSizeChanged.connect(self._SH_DocumentLayoutSizeChanged)
        self.lock_queue = deque()
        self.history = []
        self.history_index = 0  # negative indexes with 0 indicating the text being typed.
        self.stashed_content = None

    @property
    def empty(self):
        document = self.document()
        last_block = document.lastBlock()
        return document.characterCount() <= 1 and not last_block.textList()

    @property
    def locked(self):
        return bool(self.lock_queue)

    def dragEnterEvent(self, event):
        event.ignore()  # let the parent process DND

    def keyPressEvent(self, event):
        key, modifiers = event.key(), event.modifiers()
        if self.isReadOnly():
            event.ignore()
        elif key in (Qt.Key_Enter, Qt.Key_Return) and modifiers == Qt.NoModifier:
            document = self.document()
            last_block = document.lastBlock()
            if document.characterCount() > 1 or last_block.textList():
                text = self.toHtml()
                if not self.history or self.history[-1] != text:
                    self.history.append(text)
                self.history_index = 0
                self.stashed_content = None
                if document.blockCount() > 1 and not last_block.text() and not last_block.textList():
                    # prevent an extra empty line being added at the end of the text
                    cursor = self.textCursor()
                    cursor.movePosition(cursor.End)
                    cursor.deletePreviousChar()
                text = self.toHtml()
                self.clear()
                self.textEntered.emit(text)
            event.accept()
        elif key == Qt.Key_Up and modifiers == Qt.ControlModifier:
            try:
                history_entry = self.history[self.history_index - 1]
            except IndexError:
                pass
            else:
                if self.history_index == 0:
                    self.stashed_content = self.toHtml()
                self.history_index -= 1
                self.setHtml(history_entry)
            event.accept()
        elif key == Qt.Key_Down and modifiers == Qt.ControlModifier:
            if self.history_index == 0:
                pass
            elif self.history_index == -1:
                self.history_index = 0
                self.setHtml(self.stashed_content)
                self.stashed_content = None
            else:
                self.history_index += 1
                self.setHtml(self.history[self.history_index])
            event.accept()
        else:
            QTextEdit.keyPressEvent(self, event)

    def _SH_DocumentLayoutSizeChanged(self, new_size):
        self.setFixedHeight(min(new_size.height()+self.contentsMargins().top()+self.contentsMargins().bottom(), self.parent().height()/2))

    def _SH_LockWidgetConfirmButtonClicked(self):
        self.lockReleased.emit(self.lock_queue.popleft())
        if self.locked:
            lock_type = self.lock_queue[0]
            self.lock_widget.note_label.setText(lock_type.note_text)
            self.lock_widget.confirm_button.setText(lock_type.button_text)
            self.lockEngaged.emit(lock_type)
        else:
            self.lock_widget.hide()
            self.setReadOnly(False)

    def lock(self, lock_type):
        if lock_type in self.lock_queue:
            raise ValueError("already locked with {}".format(lock_type))
        if not self.locked:
            self.lock_widget.note_label.setText(lock_type.note_text)
            self.lock_widget.confirm_button.setText(lock_type.button_text)
            self.lock_widget.show()
            self.setReadOnly(True)
            self.lockEngaged.emit(lock_type)
        self.lock_queue.append(lock_type)

    def reset_locks(self):
        self.setReadOnly(False)
        self.lock_widget.hide()
        self.lock_queue.clear()

    def clear(self):
        super(ChatTextInput, self).clear()
        self.setCurrentCharFormat(QTextCharFormat())  # clear() doesn't clear the text formatting, only the content

    def setHtml(self, text):
        super(ChatTextInput, self).setHtml(text)
        cursor = self.textCursor()
        cursor.movePosition(QTextCursor.End)
        self.setTextCursor(cursor)


class IconDescriptor(object):
    def __init__(self, filename):
        self.filename = filename
        self.icon = None

    def __get__(self, instance, owner):
        if self.icon is None:
            self.icon = QIcon(self.filename)
            self.icon.filename = self.filename
        return self.icon

    def __set__(self, obj, value):
        raise AttributeError("attribute cannot be set")

    def __delete__(self, obj):
        raise AttributeError("attribute cannot be deleted")


class Thumbnail(object):
    def __new__(cls, filename):
        image_reader = QImageReader(filename)
        if image_reader.canRead() and image_reader.size().isValid():
            if image_reader.supportsAnimation() and image_reader.imageCount() > 1:
                image_format = str(image_reader.format())
                image_data = image_reader.device().read()
            else:
                file_format = str(image_reader.format())
                file_size = image_reader.device().size()
                image_size = image_reader.size()
                if image_size.height() > 720:
                    image_reader.setScaledSize(image_size * 720 / image_size.height())
                image = QPixmap.fromImageReader(image_reader)
                image_buffer = QBuffer()
                image_format = 'png' if image.hasAlphaChannel() or (file_format in {'png', 'tiff', 'ico'} and file_size <= 100*1024) else 'jpeg'
                image.save(image_buffer, image_format)
                image_data = image_buffer.data()
            instance = super(Thumbnail, cls).__new__(cls)
            instance.__dict__['data'] = image_data
            instance.__dict__['type'] = 'image/{}'.format(image_format)
        else:
            instance = None
        return instance

    @property
    def data(self):
        return self.__dict__['data']

    @property
    def type(self):
        return self.__dict__['type']


class FileDescriptor(object):
    filename  = WriteOnceAttribute()
    thumbnail = WriteOnceAttribute()

    def __init__(self, filename):
        self.filename = filename
        self.thumbnail = Thumbnail(filename)

    def __hash__(self):
        return hash(self.filename)

    def __eq__(self, other):
        if isinstance(other, FileDescriptor):
            return self.filename == other.filename
        return NotImplemented

    def __ne__(self, other):
        return not (self == other)

    def __repr__(self):
        return 'FileDescriptor({})'.format(self.filename)

    @property
    def fileurl(self):
        return QUrl.fromLocalFile(self.filename).toString()


ui_class, base_class = uic.loadUiType(Resources.get('chat_widget.ui'))


@implementer(IObserver)
class ChatWidget(base_class, ui_class):

    default_user_icon = IconDescriptor(Resources.get('icons/default-avatar.png'))

    chat_template = open(Resources.get('chat/template.html')).read()

    image_data_re = re.compile(r"data:(?P<type>image/.+?);base64,(?P<data>.*)", re.I|re.U)

    def __init__(self, session, parent=None):
        super(ChatWidget, self).__init__(parent)
        with Resources.directory:
            self.setupUi(self)
        blink_settings = BlinkSettings()
        self.style = ChatMessageStyle(blink_settings.chat_window.style)
        self.style_variant = blink_settings.chat_window.style_variant or self.style.default_variant
        self.font_family = blink_settings.chat_window.font or self.style.font_family
        self.font_size = blink_settings.chat_window.font_size or self.style.font_size
        self.user_icons_css_class = 'show-icons' if blink_settings.chat_window.show_user_icons else 'hide-icons'
        self.chat_view.setHtml(self.chat_template.format(base_url=FileURL(self.style.path)+'/', style_url=self.style_variant+'.style', font_family=self.font_family, font_size=self.font_size))
        self.chat_element = self.chat_view.page().mainFrame().findFirstElement('#chat')
        self.composing_timer = QTimer()
        self.last_message = None
        self.session = session
        if session is not None:
            notification_center = NotificationCenter()
            notification_center.add_observer(ObserverWeakrefProxy(self), sender=session.blink_session)
        # connect to signals
        self.chat_input.textChanged.connect(self._SH_ChatInputTextChanged)
        self.chat_input.textEntered.connect(self._SH_ChatInputTextEntered)
        self.chat_input.lockReleased.connect(self._SH_ChatInputLockReleased)
        self.chat_view.sizeChanged.connect(self._SH_ChatViewSizeChanged)
        self.chat_view.page().mainFrame().contentsSizeChanged.connect(self._SH_ChatViewFrameContentsSizeChanged)
        self.composing_timer.timeout.connect(self._SH_ComposingTimerTimeout)

    @property
    def user_icon(self):
        return IconManager().get('avatar') or self.default_user_icon

    def add_message(self, message):
        insertion_point = self.chat_element.findFirst('#insert')
        if message.is_related_to(self.last_message):
            message.consecutive = True
            insertion_point.replace(message.to_html(self.style, user_icons=self.user_icons_css_class))
        else:
            insertion_point.removeFromDocument()
            self.chat_element.appendInside(message.to_html(self.style, user_icons=self.user_icons_css_class))
        self.last_message = message

    def send_message(self, content, content_type='text/plain', recipients=None, courtesy_recipients=None, subject=None, timestamp=None, required=None, additional_headers=None):
        blink_session = self.session.blink_session

        if blink_session.state in ('initialized', 'ended'):
            blink_session.init_outgoing(blink_session.account, blink_session.contact, blink_session.contact_uri, [StreamDescription('chat')], reinitialize=True)
            blink_session.connect()
        elif blink_session.state == 'connected/*':
            if self.session.chat_stream is None:
                self.session.blink_session.add_stream(StreamDescription('chat'))
        elif blink_session.state == 'connecting/*' and self.session.chat_stream is not None:
            pass
        else:
            raise RuntimeError("Cannot send messages in the '%s' state" % blink_session.state)

        self.session.chat_stream.send_message(content, content_type, recipients, courtesy_recipients, subject, timestamp, required, additional_headers)

    def _align_chat(self, scroll=False):
        # frame_height = self.chat_view.page().mainFrame().contentsSize().height()
        widget_height = self.chat_view.size().height()
        content_height = self.chat_element.geometry().height()
        # print widget_height, frame_height, content_height
        if widget_height > content_height:
            self.chat_element.setStyleProperty('position', 'relative')
            self.chat_element.setStyleProperty('top', '%dpx' % (widget_height-content_height))
        else:
            self.chat_element.setStyleProperty('position', 'static')
            self.chat_element.setStyleProperty('top', None)
        frame = self.chat_view.page().mainFrame()
        if scroll or frame.scrollBarMaximum(Qt.Vertical) - frame.scrollBarValue(Qt.Vertical) <= widget_height*0.2:
            # print "scroll requested or scrollbar is closer than %dpx to the bottom" % (widget_height*0.2)
            # self._print_scrollbar_position()
            self._scroll_to_bottom()
            # self._print_scrollbar_position()

    def _scroll_to_bottom(self):
        frame = self.chat_view.page().mainFrame()
        frame.setScrollBarValue(Qt.Vertical, frame.scrollBarMaximum(Qt.Vertical))

    def _print_scrollbar_position(self):
        frame = self.chat_view.page().mainFrame()
        print("%d out of %d, %d+%d=%d (%d)" % (frame.scrollBarValue(Qt.Vertical), frame.scrollBarMaximum(Qt.Vertical), frame.scrollBarValue(Qt.Vertical), self.chat_view.size().height(),
                                               frame.scrollBarValue(Qt.Vertical)+self.chat_view.size().height(), frame.contentsSize().height()))

    def dragEnterEvent(self, event):
        mime_data = event.mimeData()
        if mime_data.hasUrls() or mime_data.hasHtml() or mime_data.hasText():
            event.accept()
        else:
            event.ignore()

    def dragLeaveEvent(self, event):
        event.accept()

    def dragMoveEvent(self, event):
        if event.possibleActions() & (Qt.CopyAction | Qt.LinkAction):
            event.accept(self.rect())
        else:
            event.ignore(self.rect())

    def dropEvent(self, event):
        event.acceptProposedAction()
        mime_data = event.mimeData()
        if mime_data.hasUrls():
            urls = mime_data.urls()
            schemes = {url.scheme() for url in urls}
            if schemes == {'file'}:
                self._DH_Files(urls)
            else:
                self._DH_Text('\n'.join(url.toString() for url in urls))
        else:
            mime_types = set(mime_data.formats())
            if mime_types.issuperset({'text/html', 'text/_moz_htmlcontext'}):
                text = str(mime_data.data('text/html'), encoding='utf16')
            else:
                text = mime_data.html() or mime_data.text()
            self._DH_Text(text)

    def _DH_Files(self, urls):
        session_manager = SessionManager()
        blink_session = self.session.blink_session

        file_descriptors  = [FileDescriptor(url.toLocalFile()) for url in urls]
        image_descriptors = [descriptor for descriptor in file_descriptors if descriptor.thumbnail is not None]
        other_descriptors = [descriptor for descriptor in file_descriptors if descriptor.thumbnail is None]

        for image in image_descriptors:
            try:
                image_data = base64.b64encode(image.thumbnail.data).decode()
                self.send_message(image_data, content_type=image.thumbnail.type)
            except Exception as e:
                self.add_message(ChatStatus("Error sending image '%s': %s" % (os.path.basename(image.filename), str(e))))  # decide what type to use here. -Dan
            else:
                content = '''<a href="{}"><img src="data:{};base64,{}" class="scaled-to-fit" /></a>'''.format(image.fileurl, image.thumbnail.type, image_data)
                sender  = ChatSender(blink_session.account.display_name, blink_session.account.id, self.user_icon.filename)
                self.add_message(ChatMessage(content, sender, 'outgoing'))

        for descriptor in other_descriptors:
            session_manager.send_file(blink_session.contact, blink_session.contact_uri, descriptor.filename, account=blink_session.account)

    def _DH_Text(self, text):
        match = self.image_data_re.match(text)
        if match is not None:
            try:
                data = match.group('data') if isinstance(match.group('data'), bytes) else match.group('data').encode()
                image_data = base64.b64encode(data).decode()
                self.send_message(image_data, content_type=match.group('type'))
            except Exception as e:
                self.add_message(ChatStatus('Error sending image: %s' % str(e)))
            else:
                account = self.session.blink_session.account
                content = '''<img src="{}" class="scaled-to-fit" />'''.format(text)
                sender  = ChatSender(account.display_name, account.id, self.user_icon.filename)
                self.add_message(ChatMessage(content, sender, 'outgoing'))
        else:
            user_text = self.chat_input.toHtml()
            self.chat_input.setHtml(text)
            self.chat_input.keyPressEvent(QKeyEvent(QEvent.KeyPress, Qt.Key_Return, Qt.NoModifier, text='\r'))
            self.chat_input.setHtml(user_text)

    def _SH_ChatViewSizeChanged(self):
        # print "chat view size changed"
        self._align_chat(scroll=True)

    def _SH_ChatViewFrameContentsSizeChanged(self, size):
        # print "frame contents size changed to %r (current=%r)" % (size, self.chat_view.page().mainFrame().contentsSize())
        self._align_chat(scroll=True)

    def _SH_ChatInputTextChanged(self):
        chat_stream = self.session.chat_stream
        if chat_stream is None:
            return
        if self.chat_input.empty:
            if self.composing_timer.isActive():
                self.composing_timer.stop()
                try:
                    chat_stream.send_composing_indication('idle')
                except Exception:
                    pass
        elif not self.composing_timer.isActive():
            try:
                chat_stream.send_composing_indication('active')
            except Exception:
                pass
            else:
                self.composing_timer.start(10000)

    def _SH_ChatInputTextEntered(self, text):
        self.composing_timer.stop()
        doc = QTextDocument()
        doc.setHtml(text)
        plain_text = doc.toPlainText()
        if plain_text == '/otr+':
            try:
                self.session.chat_stream.encryption.start()
            except AttributeError:
                pass
            return
        elif plain_text == '/otr-':
            try:
                self.session.chat_stream.encryption.stop()
            except AttributeError:
                pass
            return
        try:
            self.send_message(text, content_type='text/html')
        except Exception as e:
            self.add_message(ChatStatus('Error sending message: %s' % e))  # decide what type to use here. -Dan
        else:
            account = self.session.blink_session.account
            content = HtmlProcessor.autolink(text)
            sender  = ChatSender(account.display_name, account.id, self.user_icon.filename)
            self.add_message(ChatMessage(content, sender, 'outgoing'))

    def _SH_ChatInputLockReleased(self, lock_type):
        if lock_type is EncryptionLock:
            self.session.chat_stream.encryption.stop()

    def _SH_ComposingTimerTimeout(self):
        self.composing_timer.stop()
        chat_stream = self.session.chat_stream or Null
        try:
            chat_stream.send_composing_indication('idle')
        except Exception:
            pass

    @run_in_gui_thread
    def handle_notification(self, notification):
        handler = getattr(self, '_NH_%s' % notification.name, Null)
        handler(notification)

    def _NH_BlinkSessionDidEnd(self, notification):
        self.composing_timer.stop()
        self.chat_input.reset_locks()

    def _NH_BlinkSessionWasDeleted(self, notification):
        self.setParent(None)

    def _NH_BlinkSessionDidRemoveStream(self, notification):
        if notification.data.stream.type == 'chat':
            self.composing_timer.stop()
            self.chat_input.reset_locks()

del ui_class, base_class


class VideoToolButton(QToolButton):
    active = QtDynamicProperty('active', bool)

    def event(self, event):
        if event.type() == QEvent.DynamicPropertyChange and event.propertyName() == 'active':
            self.setVisible(self.active)
        return super(VideoToolButton, self).event(event)


ui_class, base_class = uic.loadUiType(Resources.get('video_widget.ui'))


@implementer(IObserver)
class VideoWidget(VideoSurface, ui_class):

    def __init__(self, session_item, parent=None):
        super(VideoWidget, self).__init__(parent)
        with Resources.directory:
            self.setupUi()
        self.session_item = session_item
        self.blink_session = session_item.blink_session
        self.parent_widget = parent
        self.setWindowFlags(Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint)
        self.fullscreen_button.clicked.connect(self._SH_FullscreenButtonClicked)
        self.screenshot_button.clicked.connect(self._SH_ScreenshotButtonClicked)
        self.detach_button.clicked.connect(self._SH_DetachButtonClicked)
        self.mute_button.clicked.connect(self._SH_MuteButtonClicked)
        self.hold_button.clicked.connect(self._SH_HoldButtonClicked)
        self.close_button.clicked.connect(self._SH_CloseButtonClicked)
        self.screenshot_button.customContextMenuRequested.connect(self._SH_ScreenshotButtonContextMenuRequested)
        self.camera_preview.adjusted.connect(self._SH_CameraPreviewAdjusted)
        self.detach_animation.finished.connect(self._SH_DetachAnimationFinished)
        self.preview_animation.finished.connect(self._SH_PreviewAnimationFinished)
        self.idle_timer.timeout.connect(self._SH_IdleTimerTimeout)
        if parent is not None:
            parent.installEventFilter(self)
            self.setGeometry(self.geometryHint())
            self.setVisible('video' in session_item.blink_session.streams)
        settings = SIPSimpleSettings()
        notification_center = NotificationCenter()
        notification_center.add_observer(ObserverWeakrefProxy(self), sender=session_item.blink_session)
        notification_center.add_observer(ObserverWeakrefProxy(self), name='CFGSettingsObjectDidChange', sender=settings)
        notification_center.add_observer(ObserverWeakrefProxy(self), name='VideoStreamRemoteFormatDidChange')
        notification_center.add_observer(ObserverWeakrefProxy(self), name='VideoStreamReceivedKeyFrame')
        notification_center.add_observer(ObserverWeakrefProxy(self), name='VideoDeviceDidChangeCamera')

    def setupUi(self):
        super(VideoWidget, self).setupUi(self)

        self.no_flicker_widget = QLabel()
        self.no_flicker_widget.setWindowFlags(Qt.FramelessWindowHint)
        # self.no_flicker_widget.setWindowFlags(Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint)

        self.camera_preview = VideoSurface(self, framerate=10)
        self.camera_preview.interactive = True
        self.camera_preview.mirror = True
        self.camera_preview.setMinimumHeight(45)
        self.camera_preview.setMaximumHeight(135)
        self.camera_preview.setGeometry(QRect(0, 0, self.camera_preview.width_for_height(81), 81))
        self.camera_preview.lower()
        self.camera_preview.scale_factor = 1.0

        self.detach_animation = None
        self.detach_animation = QPropertyAnimation(self, b'geometry')
        self.detach_animation.setDuration(200)
        self.detach_animation.setEasingCurve(QEasingCurve.Linear)

        self.preview_animation = None
        self.preview_animation = QPropertyAnimation(self.camera_preview, b'geometry')
        self.preview_animation.setDuration(500)
        self.preview_animation.setDirection(QPropertyAnimation.Forward)
        self.preview_animation.setEasingCurve(QEasingCurve.OutQuad)

        self.idle_timer = QTimer()
        self.idle_timer.setSingleShot(True)
        self.idle_timer.setInterval(3000)

        for button in self.tool_buttons:
            button.setCursor(Qt.ArrowCursor)
            button.installEventFilter(self)
            button.active = False

        # fix the SVG icons, as the generated code loads them as pixmaps, losing their ability to scale -Dan
        fullscreen_icon = QIcon()
        fullscreen_icon.addFile(Resources.get('icons/fullscreen.svg'), mode=QIcon.Normal, state=QIcon.Off)
        fullscreen_icon.addFile(Resources.get('icons/fullscreen-exit.svg'), mode=QIcon.Normal, state=QIcon.On)
        fullscreen_icon.addFile(Resources.get('icons/fullscreen-exit.svg'), mode=QIcon.Active, state=QIcon.On)
        fullscreen_icon.addFile(Resources.get('icons/fullscreen-exit.svg'), mode=QIcon.Disabled, state=QIcon.On)
        fullscreen_icon.addFile(Resources.get('icons/fullscreen-exit.svg'), mode=QIcon.Selected, state=QIcon.On)

        detach_icon = QIcon()
        detach_icon.addFile(Resources.get('icons/detach.svg'), mode=QIcon.Normal, state=QIcon.Off)
        detach_icon.addFile(Resources.get('icons/attach.svg'), mode=QIcon.Normal, state=QIcon.On)
        detach_icon.addFile(Resources.get('icons/attach.svg'), mode=QIcon.Active, state=QIcon.On)
        detach_icon.addFile(Resources.get('icons/attach.svg'), mode=QIcon.Disabled, state=QIcon.On)
        detach_icon.addFile(Resources.get('icons/attach.svg'), mode=QIcon.Selected, state=QIcon.On)

        mute_icon = QIcon()
        mute_icon.addFile(Resources.get('icons/mic-on.svg'), mode=QIcon.Normal, state=QIcon.Off)
        mute_icon.addFile(Resources.get('icons/mic-off.svg'), mode=QIcon.Normal, state=QIcon.On)
        mute_icon.addFile(Resources.get('icons/mic-off.svg'), mode=QIcon.Active, state=QIcon.On)
        mute_icon.addFile(Resources.get('icons/mic-off.svg'), mode=QIcon.Disabled, state=QIcon.On)
        mute_icon.addFile(Resources.get('icons/mic-off.svg'), mode=QIcon.Selected, state=QIcon.On)

        hold_icon = QIcon()
        hold_icon.addFile(Resources.get('icons/pause.svg'), mode=QIcon.Normal, state=QIcon.Off)
        hold_icon.addFile(Resources.get('icons/paused.svg'), mode=QIcon.Normal, state=QIcon.On)
        hold_icon.addFile(Resources.get('icons/paused.svg'), mode=QIcon.Active, state=QIcon.On)
        hold_icon.addFile(Resources.get('icons/paused.svg'), mode=QIcon.Disabled, state=QIcon.On)
        hold_icon.addFile(Resources.get('icons/paused.svg'), mode=QIcon.Selected, state=QIcon.On)

        screenshot_icon = QIcon()
        screenshot_icon.addFile(Resources.get('icons/screenshot.svg'), mode=QIcon.Normal, state=QIcon.Off)

        close_icon = QIcon()
        close_icon.addFile(Resources.get('icons/close.svg'), mode=QIcon.Normal, state=QIcon.Off)
        close_icon.addFile(Resources.get('icons/close-active.svg'), mode=QIcon.Active, state=QIcon.Off)

        self.fullscreen_button.setIcon(fullscreen_icon)
        self.screenshot_button.setIcon(screenshot_icon)
        self.detach_button.setIcon(detach_icon)
        self.mute_button.setIcon(mute_icon)
        self.hold_button.setIcon(hold_icon)
        self.close_button.setIcon(close_icon)

        self.screenshot_button_menu = QMenu(self)
        self.screenshot_button_menu.addAction('Open screenshots folder', self._SH_ScreenshotsFolderActionTriggered)

    @property
    def interactive(self):
        return self.parent() is None and not self.isFullScreen()

    @property
    def tool_buttons(self):
        return tuple(attr for attr in vars(self).values() if isinstance(attr, VideoToolButton))

    @property
    def active_tool_buttons(self):
        return tuple(button for button in self.tool_buttons if button.active)

    def eventFilter(self, watched, event):
        event_type = event.type()
        if watched is self.parent():
            if event_type == QEvent.Resize:
                self.setGeometry(self.geometryHint())
        elif event_type == QEvent.Enter:
            self.idle_timer.stop()
            cursor = self.cursor()
            cursor_pos = cursor.pos()
            if not watched.rect().translated(watched.mapToGlobal(QPoint(0, 0))).contains(cursor_pos):
                # sometimes we get invalid enter events for the fullscreen_button after we switch to fullscreen.
                # simulate a mouse move in and out of the button to force qt to update the button state.
                cursor.setPos(self.mapToGlobal(watched.geometry().center()))
                cursor.setPos(cursor_pos)
        elif event_type == QEvent.Leave:
            self.idle_timer.start()
        return False

    def mousePressEvent(self, event):
        super(VideoWidget, self).mousePressEvent(event)
        if self._interaction.active:
            for button in self.active_tool_buttons:
                button.show()  # show or hide the tool buttons while we move/resize? -Dan
            self.idle_timer.stop()

    def mouseReleaseEvent(self, event):
        if self._interaction.active:
            for button in self.active_tool_buttons:
                button.show()
            self.idle_timer.start()
        super(VideoWidget, self).mouseReleaseEvent(event)

    def mouseMoveEvent(self, event):
        super(VideoWidget, self).mouseMoveEvent(event)
        if self._interaction.active:
            return
        if not self.idle_timer.isActive():
            for button in self.active_tool_buttons:
                button.show()
            self.setCursor(Qt.ArrowCursor)
        self.idle_timer.start()

    def resizeEvent(self, event):
        if self.preview_animation and self.preview_animation.state() == QPropertyAnimation.Running:
            return

        if not event.oldSize().isValid():
            return

        if self.camera_preview.size() == event.oldSize():
            self.camera_preview.resize(event.size())
            return

        old_size = QSizeF(event.oldSize())
        new_size = QSizeF(event.size())

        ratio = new_size.height() / old_size.height()

        if ratio == 1:
            return

        scaled_preview_geometry = QRectF(QPointF(self.camera_preview.geometry().topLeft()) * ratio, QSizeF(self.camera_preview.size()) * ratio)
        preview_center = scaled_preview_geometry.center()
        ideal_geometry = scaled_preview_geometry.toAlignedRect()

        if ideal_geometry.right() > self.rect().right():
            ideal_geometry.moveRight(self.rect().right())
        if ideal_geometry.bottom() > self.rect().bottom():
            ideal_geometry.moveBottom(self.rect().bottom())

        new_height = limit((new_size.height() + 117) / 6 * self.camera_preview.scale_factor, min=self.camera_preview.minimumHeight(), max=self.camera_preview.maximumHeight())
        preview_geometry = QRect(0, 0, self.width_for_height(new_height), new_height)

        quadrant = QRectF(QPointF(0, 0), new_size/3)

        if quadrant.translated(0, 0).contains(preview_center):                                      # top left gravity
            preview_geometry.moveTopLeft(ideal_geometry.topLeft())
        elif quadrant.translated(quadrant.width(), 0).contains(preview_center):                     # top gravity
            preview_geometry.moveCenter(ideal_geometry.center())
            preview_geometry.moveTop(ideal_geometry.top())
        elif quadrant.translated(2*quadrant.width(), 0).contains(preview_center):                   # top right gravity
            preview_geometry.moveTopRight(ideal_geometry.topRight())

        elif quadrant.translated(0, quadrant.height()).contains(preview_center):                    # left gravity
            preview_geometry.moveCenter(ideal_geometry.center())
            preview_geometry.moveLeft(ideal_geometry.left())
        elif quadrant.translated(quadrant.width(), quadrant.height()).contains(preview_center):     # center gravity
            preview_geometry.moveCenter(ideal_geometry.center())
        elif quadrant.translated(2*quadrant.width(), quadrant.height()).contains(preview_center):   # right gravity
            preview_geometry.moveCenter(ideal_geometry.center())
            preview_geometry.moveRight(ideal_geometry.right())

        elif quadrant.translated(0, 2*quadrant.height()).contains(preview_center):                  # bottom left gravity
            preview_geometry.moveBottomLeft(ideal_geometry.bottomLeft())
        elif quadrant.translated(quadrant.width(), 2*quadrant.height()).contains(preview_center):   # bottom gravity
            preview_geometry.moveCenter(ideal_geometry.center())
            preview_geometry.moveBottom(ideal_geometry.bottom())
        elif quadrant.translated(2*quadrant.width(), 2*quadrant.height()).contains(preview_center): # bottom right gravity
            preview_geometry.moveBottomRight(ideal_geometry.bottomRight())

        self.camera_preview.setGeometry(preview_geometry)

    def setParent(self, parent):
        old_parent = self.parent()
        if old_parent is not None:
            old_parent.removeEventFilter(self)
        super(VideoWidget, self).setParent(parent)
        if parent is not None:
            parent.installEventFilter(self)
            self.setGeometry(self.geometryHint())

    def setVisible(self, visible):
        if visible == False and self.isFullScreen():
            self.showNormal()
            if not self.detach_button.isChecked():
                self.setParent(self.parent_widget)
                self.setGeometry(self.parent().rect())
            self.fullscreen_button.setChecked(False)
        super(VideoWidget, self).setVisible(visible)

    def geometryHint(self, parent=None):
        parent = parent or self.parent()
        if parent is not None:
            origin = QPoint(0, 0)
            size   = QSize(parent.width(), min(self.height_for_width(parent.width()), parent.height() - 175))
        else:
            origin = self.geometry().topLeft()
            size   = QSize(self.width_for_height(self.height()), self.height())
        return QRect(origin, size)

    @run_in_gui_thread
    def handle_notification(self, notification):
        handler = getattr(self, '_NH_%s' % notification.name, Null)
        handler(notification)

    def _NH_BlinkSessionWillConnect(self, notification):
        if 'video' in notification.sender.streams:
            self.setParent(self.parent_widget)
            self.setGeometry(self.geometryHint())
            self.detach_button.setChecked(False)
            for button in self.tool_buttons:
                button.active = False
            self.camera_preview.setMaximumHeight(16777215)
            self.camera_preview.setGeometry(self.rect())
            self.camera_preview.setCursor(Qt.ArrowCursor)
            self.camera_preview.interactive = False
            self.camera_preview.scale_factor = 1.0
            self.camera_preview.producer = SIPApplication.video_device.producer
            self.setCursor(Qt.ArrowCursor)
            self.show()

    def _NH_BlinkSessionDidConnect(self, notification):
        video_stream = notification.sender.streams.get('video')
        if video_stream is not None:
            if self.parent() is None:
                self.setParent(self.parent_widget)
                self.setGeometry(self.geometryHint())
                self.detach_button.setChecked(False)
            for button in self.tool_buttons:
                button.active = False
            self.camera_preview.setMaximumHeight(16777215)
            self.camera_preview.setGeometry(self.rect())
            self.camera_preview.setCursor(Qt.ArrowCursor)
            self.camera_preview.interactive = False
            self.camera_preview.scale_factor = 1.0
            self.camera_preview.producer = SIPApplication.video_device.producer
            self.producer = video_stream.producer
            self.setCursor(Qt.ArrowCursor)
            self.show()
        else:
            self.hide()
            self.producer = None
            self._image = None
            self.camera_preview.producer = None
            self.camera_preview._image = None

    def _NH_BlinkSessionWillAddStream(self, notification):
        if notification.data.stream.type == 'video':
            self.setParent(self.parent_widget)
            self.setGeometry(self.geometryHint())
            self.detach_button.setChecked(False)
            for button in self.tool_buttons:
                button.active = False
            self.camera_preview.setMaximumHeight(16777215)
            self.camera_preview.setGeometry(self.rect())
            self.camera_preview.setCursor(Qt.ArrowCursor)
            self.camera_preview.interactive = False
            self.camera_preview.scale_factor = 1.0
            self.camera_preview.producer = SIPApplication.video_device.producer
            self.setCursor(Qt.ArrowCursor)
            self.show()

    def _NH_BlinkSessionDidAddStream(self, notification):
        if notification.data.stream.type == 'video':
            self.producer = notification.data.stream.producer

    def _NH_BlinkSessionDidNotAddStream(self, notification):
        if notification.data.stream.type == 'video':
            self.hide()
            self.producer = None
            self._image = None
            self.camera_preview.producer = None
            self.camera_preview._image = None

    def _NH_BlinkSessionDidRemoveStream(self, notification):
        if notification.data.stream.type == 'video':
            self.hide()
            self.producer = None
            self._image = None
            self.camera_preview.producer = None
            self.camera_preview._image = None

    def _NH_BlinkSessionDidEnd(self, notification):
        self.hide()
        self.producer = None
        self._image = None
        self.camera_preview.producer = None
        self.camera_preview._image = None

    def _NH_BlinkSessionWasDeleted(self, notification):
        self.stop()
        self.setParent(None)
        self.session_item = None
        self.blink_session = None
        self.parent_widget = None
        self.detach_animation = None
        self.preview_animation = None

    def _NH_BlinkSessionDidChangeHoldState(self, notification):
        self.hold_button.setChecked(notification.data.local_hold)

    def _NH_VideoStreamRemoteFormatDidChange(self, notification):
        if notification.sender.blink_session is self.blink_session and not self.isFullScreen():
            self.setGeometry(self.geometryHint())

    def _NH_VideoStreamReceivedKeyFrame(self, notification):
        if notification.sender.blink_session is self.blink_session and self.preview_animation and self.preview_animation.state() != QPropertyAnimation.Running and self.camera_preview.size() == self.size():
            if self.preview_animation:
	            self.preview_animation.setStartValue(self.rect())
	            self.preview_animation.setEndValue(QRect(0, 0, self.camera_preview.width_for_height(81), 81))
	            self.preview_animation.start()

    def _NH_VideoDeviceDidChangeCamera(self, notification):
        # self.camera_preview.producer = SIPApplication.video_device.producer
        self.camera_preview.producer = notification.data.new_camera

    def _NH_CFGSettingsObjectDidChange(self, notification):
        settings = SIPSimpleSettings()
        if 'audio.muted' in notification.data.modified:
            self.mute_button.setChecked(settings.audio.muted)

    def _SH_CameraPreviewAdjusted(self, old_geometry, new_geometry):
        if new_geometry.size() != old_geometry.size():
            default_height_for_size = (self.height() + 117) / 6
            self.camera_preview.scale_factor = new_geometry.height() / default_height_for_size

    def _SH_IdleTimerTimeout(self):
        for button in self.active_tool_buttons:
            button.hide()
        self.setCursor(Qt.BlankCursor)

    def _SH_FullscreenButtonClicked(self, checked):
        if checked:
            if not self.detach_button.isChecked():
                geometry = self.rect().translated(self.mapToGlobal(QPoint(0, 0)))
                self.setParent(None)
                self.setGeometry(geometry)
                self.show()  # without this, showFullScreen below doesn't work properly
            self.detach_button.active = False
            self.mute_button.active = True
            self.hold_button.active = True
            self.close_button.active = True
            self.showFullScreen()
            self.fullscreen_button.hide()  # it seems the leave event after the button is pressed doesn't register and starting the idle timer here doesn't work well either -Dan
            self.fullscreen_button.show()
        else:
            if not self.detach_button.isChecked():
                self.setGeometry(self.geometryHint(self.parent_widget))  # force a geometry change before re-parenting, else we will get a change from (-1, -1) to the parent geometry hint
                self.setParent(self.parent_widget)                       # this is probably because since it unmaps when it's re-parented, the geometry change won't appear from fullscreen
                self.setGeometry(self.geometryHint())                    # to the new size, since we changed the geometry after returning from fullscreen, while invisible
                self.mute_button.active = False
                self.hold_button.active = False
                self.close_button.active = False
            self.detach_button.active = True
            self.showNormal()
            self.window().show()
        self.setCursor(Qt.ArrowCursor)

    def _SH_DetachButtonClicked(self, checked):
        if checked:
            if self.isFullScreen():
                self.showNormal()

            desktop = QApplication.desktop()
            screen_area = desktop.availableGeometry(self)

            start_rect = self.rect()
            final_rect = QRect(0, 0, self.width_for_height(261), 261)
            start_geometry = start_rect.translated(self.mapToGlobal(QPoint(0, 0)))
            final_geometry = final_rect.translated(screen_area.topRight() - final_rect.topRight() + QPoint(-10, 10))

            pixmap = self.grab()
            self.no_flicker_widget.resize(pixmap.size())
            self.no_flicker_widget.setPixmap(pixmap)
            self.no_flicker_widget.setGeometry(self.rect().translated(self.mapToGlobal(QPoint(0, 0))))
            self.no_flicker_widget.show()
            self.no_flicker_widget.raise_()

            self.setParent(None)
            self.setGeometry(start_geometry)
            self.show()
            self.no_flicker_widget.hide()

            self.detach_animation.setDirection(QPropertyAnimation.Forward)
            self.detach_animation.setEasingCurve(QEasingCurve.OutQuad)
            self.detach_animation.setStartValue(start_geometry)
            self.detach_animation.setEndValue(final_geometry)
            self.detach_animation.start()
        else:
            start_geometry = self.geometry()
            final_geometry = self.geometryHint(self.parent_widget).translated(self.parent_widget.mapToGlobal(QPoint(0, 0)))

            # do this early or late? -Dan
            self.parent_widget.window().show()

            self.detach_animation.setDirection(QPropertyAnimation.Backward)
            self.detach_animation.setEasingCurve(QEasingCurve.InQuad)
            self.detach_animation.setStartValue(final_geometry)  # start and end are reversed because we go backwards
            self.detach_animation.setEndValue(start_geometry)
            self.detach_animation.start()
        self.fullscreen_button.setChecked(False)

    def _SH_ScreenshotButtonClicked(self):
        screenshot = VideoScreenshot(self)
        screenshot.capture()
        screenshot.save()

    def _SH_MuteButtonClicked(self, checked):
        settings = SIPSimpleSettings()
        settings.audio.muted = checked
        settings.save()

    def _SH_HoldButtonClicked(self, checked):
        if checked:
            self.blink_session.hold()
        else:
            self.blink_session.unhold()

    def _SH_CloseButtonClicked(self):
        if 'screen-sharing' in self.blink_session.streams:
            self.blink_session.remove_stream(self.session_item.video_stream)
        else:
            self.session_item.end()

    def _SH_ScreenshotButtonContextMenuRequested(self, pos):
        if not self.isFullScreen():
            self.screenshot_button_menu.exec_(self.screenshot_button.mapToGlobal(pos))

    def _SH_ScreenshotsFolderActionTriggered(self):
        settings = BlinkSettings()
        QDesktopServices.openUrl(QUrl.fromLocalFile(settings.screenshots_directory.normalized))

    def _SH_DetachAnimationFinished(self):
        if self.detach_animation.direction() == QPropertyAnimation.Backward:
            pixmap = self.grab()
            self.no_flicker_widget.resize(pixmap.size())
            self.no_flicker_widget.setPixmap(pixmap)
            self.no_flicker_widget.setGeometry(self.geometry())
            self.no_flicker_widget.show()
            self.no_flicker_widget.raise_()
            # self.no_flicker_widget.repaint()
            # self.repaint()
            self.setParent(self.parent_widget)
            self.setGeometry(self.geometryHint())
            self.show() # solve the flicker -Dan
            # self.repaint()
            # self.no_flicker_widget.lower()
            self.no_flicker_widget.hide()
            # self.window().show()
            self.mute_button.active = False
            self.hold_button.active = False
            self.close_button.active = False
        else:
            self.detach_button.hide()  # it seems the leave event after the button is pressed doesn't register and starting the idle timer here doesn't work well either -Dan
            self.detach_button.show()
            self.mute_button.active = True
            self.hold_button.active = True
            self.close_button.active = True
        self.setCursor(Qt.ArrowCursor)

    def _SH_PreviewAnimationFinished(self):
        self.camera_preview.setMaximumHeight(135)
        self.camera_preview.interactive = True
        self.setCursor(Qt.ArrowCursor)
        self.detach_button.active = True
        self.fullscreen_button.active = True
        self.screenshot_button.active = True
        self.idle_timer.start()

del ui_class, base_class


class NoSessionsLabel(QLabel):
    def __init__(self, chat_window):
        super(NoSessionsLabel, self).__init__(chat_window.session_panel)
        self.chat_window = chat_window
        font = self.font()
        font.setPointSize(20)
        self.setFont(font)
        self.setAlignment(Qt.AlignCenter)
        self.setStyleSheet("""QLabel { border: 1px inset palette(dark); border-radius: 3px; background-color: white; color: #545454; }""")
        self.setText("No Sessions")
        chat_window.session_panel.installEventFilter(self)

    def eventFilter(self, watched, event):
        if event.type() == QEvent.Resize:
            self.resize(event.size())
        return False


ui_class, base_class = uic.loadUiType(Resources.get('chat_window.ui'))


@implementer(IObserver)
class ChatWindow(base_class, ui_class, ColorHelperMixin):

    sliding_panels = True

    __streamtypes__ = {'chat', 'screen-sharing', 'video'} # the stream types for which we show the chat window

    def __init__(self, parent=None):
        super(ChatWindow, self).__init__(parent)
        with Resources.directory:
            self.setupUi()

        self.selected_item = None
        self.session_model = ChatSessionModel(self)
        self.session_list.setModel(self.session_model)
        self.session_widget.installEventFilter(self)
        self.state_label.installEventFilter(self)

        self.info_panel.installEventFilter(self)
        self.audio_encryption_label.installEventFilter(self)
        self.video_encryption_label.installEventFilter(self)
        self.chat_encryption_label.installEventFilter(self)

        self.latency_graph.installEventFilter(self)
        self.packet_loss_graph.installEventFilter(self)
        self.traffic_graph.installEventFilter(self)

        self.mute_button.clicked.connect(self._SH_MuteButtonClicked)
        self.hold_button.clicked.connect(self._SH_HoldButtonClicked)
        self.record_button.clicked.connect(self._SH_RecordButtonClicked)
        self.control_button.clicked.connect(self._SH_ControlButtonClicked)
        self.participants_panel_info_button.clicked.connect(self._SH_InfoButtonClicked)
        self.participants_panel_files_button.clicked.connect(self._SH_FilesButtonClicked)
        self.files_panel_info_button.clicked.connect(self._SH_InfoButtonClicked)
        self.files_panel_participants_button.clicked.connect(self._SH_ParticipantsButtonClicked)
        self.info_panel_files_button.clicked.connect(self._SH_FilesButtonClicked)
        self.info_panel_participants_button.clicked.connect(self._SH_ParticipantsButtonClicked)
        self.latency_graph.updated.connect(self._SH_LatencyGraphUpdated)
        self.packet_loss_graph.updated.connect(self._SH_PacketLossGraphUpdated)
        self.traffic_graph.updated.connect(self._SH_TrafficGraphUpdated)
        self.session_model.sessionAdded.connect(self._SH_SessionModelSessionAdded)
        self.session_model.sessionRemoved.connect(self._SH_SessionModelSessionRemoved)
        self.session_model.sessionAboutToBeRemoved.connect(self._SH_SessionModelSessionAboutToBeRemoved)
        self.session_list.selectionModel().selectionChanged.connect(self._SH_SessionListSelectionChanged)
        self.otr_widget.nameChanged.connect(self._SH_OTRWidgetNameChanged)
        self.otr_widget.statusChanged.connect(self._SH_OTRWidgetStatusChanged)
        self.zrtp_widget.nameChanged.connect(self._SH_ZRTPWidgetNameChanged)
        self.zrtp_widget.statusChanged.connect(self._SH_ZRTPWidgetStatusChanged)

        geometry = QSettings().value("chat_window/geometry")
        if geometry:
            self.restoreGeometry(geometry)

        notification_center = NotificationCenter()
        notification_center.add_observer(self, name='SIPApplicationDidStart')
        notification_center.add_observer(self, name='BlinkSessionNewIncoming')
        notification_center.add_observer(self, name='BlinkSessionNewOutgoing')
        notification_center.add_observer(self, name='BlinkSessionDidReinitializeForIncoming')
        notification_center.add_observer(self, name='BlinkSessionDidReinitializeForOutgoing')
        notification_center.add_observer(self, name='ChatStreamGotMessage')
        notification_center.add_observer(self, name='ChatStreamGotComposingIndication')
        notification_center.add_observer(self, name='ChatStreamDidSendMessage')
        notification_center.add_observer(self, name='ChatStreamDidDeliverMessage')
        notification_center.add_observer(self, name='ChatStreamDidNotDeliverMessage')
        notification_center.add_observer(self, name='ChatStreamOTREncryptionStateChanged')
        notification_center.add_observer(self, name='ChatStreamOTRError')
        notification_center.add_observer(self, name='MediaStreamDidInitialize')
        notification_center.add_observer(self, name='MediaStreamDidNotInitialize')
        notification_center.add_observer(self, name='MediaStreamDidStart')
        notification_center.add_observer(self, name='MediaStreamDidFail')
        notification_center.add_observer(self, name='MediaStreamDidEnd')
        notification_center.add_observer(self, name='MediaStreamWillEnd')

        # self.splitter.splitterMoved.connect(self._SH_SplitterMoved) # check this and decide on what size to have in the window (see Notes) -Dan

    def _SH_SplitterMoved(self, pos, index):
        print("-- splitter:", pos, index, self.splitter.sizes())

    def setupUi(self):
        super(ChatWindow, self).setupUi(self)

        self.session_list = ChatSessionListView(self)
        self.session_list.setObjectName('session_list')

        self.no_sessions_label = NoSessionsLabel(self)
        self.no_sessions_label.setObjectName('no_sessions_label')

        self.otr_widget = OTRWidget(self.info_panel)
        self.zrtp_widget = ZRTPWidget(self.info_panel)
        self.zrtp_widget.stream_type = None

        self.control_icon = QIcon(Resources.get('icons/cog.svg'))
        self.cancel_icon = QIcon(Resources.get('icons/cancel.png'))

        self.pixmaps = Container()

        self.pixmaps.direct_connection = QPixmap(Resources.get('icons/connection-direct.svg'))
        self.pixmaps.relay_connection = QPixmap(Resources.get('icons/connection-relay.svg'))
        self.pixmaps.unknown_connection = QPixmap(Resources.get('icons/connection-unknown.svg'))

        self.pixmaps.blue_lock = QPixmap(Resources.get('icons/lock-blue-12.svg'))
        self.pixmaps.grey_lock = QPixmap(Resources.get('icons/lock-grey-12.svg'))
        self.pixmaps.green_lock = QPixmap(Resources.get('icons/lock-green-12.svg'))
        self.pixmaps.orange_lock = QPixmap(Resources.get('icons/lock-orange-12.svg'))

        def blended_pixmap(pixmap, color):
            blended_pixmap = QPixmap(pixmap)
            painter = QPainter(blended_pixmap)
            painter.setRenderHint(QPainter.Antialiasing, True)
            painter.setCompositionMode(QPainter.CompositionMode_SourceAtop)
            painter.fillRect(blended_pixmap.rect(), color)
            painter.end()
            return blended_pixmap

        color = QColor(255, 255, 255, 64)
        self.pixmaps.light_blue_lock = blended_pixmap(self.pixmaps.blue_lock, color)
        self.pixmaps.light_grey_lock = blended_pixmap(self.pixmaps.grey_lock, color)
        self.pixmaps.light_green_lock = blended_pixmap(self.pixmaps.green_lock, color)
        self.pixmaps.light_orange_lock = blended_pixmap(self.pixmaps.orange_lock, color)

        # fix the SVG icons as the generated code loads them as pixmaps, losing their ability to scale -Dan
        def svg_icon(filename_off, filename_on):
            icon = QIcon()
            icon.addFile(filename_off, mode=QIcon.Normal, state=QIcon.Off)
            icon.addFile(filename_on,  mode=QIcon.Normal, state=QIcon.On)
            icon.addFile(filename_on,  mode=QIcon.Active, state=QIcon.On)
            return icon

        self.mute_button.setIcon(svg_icon(Resources.get('icons/mic-on.svg'), Resources.get('icons/mic-off.svg')))
        self.hold_button.setIcon(svg_icon(Resources.get('icons/pause.svg'), Resources.get('icons/paused.svg')))
        self.record_button.setIcon(svg_icon(Resources.get('icons/record.svg'), Resources.get('icons/recording.svg')))
        self.control_button.setIcon(self.control_icon)

        self.control_menu = QMenu(self.control_button)
        self.control_button.setMenu(self.control_menu)
        self.control_button.actions = ContextMenuActions()
        self.control_button.actions.connect = QAction("Connect", self, triggered=self._AH_Connect)
        self.control_button.actions.connect_with_audio = QAction("Connect with audio", self, triggered=self._AH_ConnectWithAudio)
        self.control_button.actions.connect_with_video = QAction("Connect with video", self, triggered=self._AH_ConnectWithVideo)
        self.control_button.actions.disconnect = QAction("Disconnect", self, triggered=self._AH_Disconnect)
        self.control_button.actions.add_audio = QAction("Add audio", self, triggered=self._AH_AddAudio)
        self.control_button.actions.remove_audio = QAction("Remove audio", self, triggered=self._AH_RemoveAudio)
        self.control_button.actions.add_video = QAction("Add video", self, triggered=self._AH_AddVideo)
        self.control_button.actions.remove_video = QAction("Remove video", self, triggered=self._AH_RemoveVideo)
        self.control_button.actions.share_my_screen = QAction("Share my screen", self, triggered=self._AH_ShareMyScreen)
        self.control_button.actions.request_screen = QAction("Request screen", self, triggered=self._AH_RequestScreen)
        self.control_button.actions.end_screen_sharing = QAction("End screen sharing", self, triggered=self._AH_EndScreenSharing)
        self.control_button.actions.main_window = QAction("Main Window", self, triggered=self._AH_MainWindow, shortcut='Ctrl+B', shortcutContext=Qt.ApplicationShortcut)

        self.addAction(self.control_button.actions.main_window)  # make this active even when it's not in the control_button's menu

        self.slide_direction = self.session_details.RightToLeft  # decide if we slide from one direction only -Dan
        self.slide_direction = self.session_details.Automatic
        self.session_details.animationDuration = 300
        self.session_details.animationEasingCurve = QEasingCurve.OutCirc

        self.audio_latency_graph = Graph([], color=QColor(0, 100, 215), over_boundary_color=QColor(255, 0, 100))
        self.video_latency_graph = Graph([], color=QColor(0, 215, 100), over_boundary_color=QColor(255, 100, 0), enabled=False)     # disable for now
        self.audio_packet_loss_graph = Graph([], color=QColor(0, 100, 215), over_boundary_color=QColor(255, 0, 100))
        self.video_packet_loss_graph = Graph([], color=QColor(0, 215, 100), over_boundary_color=QColor(255, 100, 0), enabled=False) # disable for now

        self.incoming_traffic_graph = Graph([], color=QColor(255, 50, 50))
        self.outgoing_traffic_graph = Graph([], color=QColor(0, 100, 215))

        self.latency_graph.add_graph(self.audio_latency_graph)
        self.latency_graph.add_graph(self.video_latency_graph)
        self.packet_loss_graph.add_graph(self.audio_packet_loss_graph)
        self.packet_loss_graph.add_graph(self.video_packet_loss_graph)

        # the graph added 2nd will be displayed on top
        self.traffic_graph.add_graph(self.incoming_traffic_graph)
        self.traffic_graph.add_graph(self.outgoing_traffic_graph)

        self.dummy_tab = None    # will be replaced by a dummy ChatWidget during SIPApplicationDidStart (creating a ChatWidget needs access to settings)
        self.tab_widget.clear()  # remove the tab(s) added in designer
        self.tab_widget.tabBar().hide()

        self.session_list.hide()

        self.otr_widget.hide()
        self.zrtp_widget.hide()
        self.info_panel_files_button.hide()
        self.info_panel_participants_button.hide()
        self.participants_panel_files_button.hide()

        self.new_messages_button.hide()
        self.hold_button.hide()
        self.record_button.hide()
        self.control_button.setEnabled(False)

        self.info_label.setForegroundRole(QPalette.Dark)

        # prepare the RTP stream encryption labels so we can take over their behaviour
        self.audio_encryption_label.hovered = False
        self.video_encryption_label.hovered = False
        self.audio_encryption_label.stream_type = 'audio'
        self.video_encryption_label.stream_type = 'video'

        self.chat_encryption_label.hovered = False

        # prepare self.session_widget so we can take over some of its painting and behaviour
        self.session_widget.setAttribute(Qt.WA_Hover, True)
        self.session_widget.hovered = False

    def _get_selected_session(self):
        return self.__dict__['selected_session']

    def _set_selected_session(self, session):
        old_session = self.__dict__.get('selected_session', None)
        new_session = self.__dict__['selected_session'] = session
        if new_session != old_session:
            self.otr_widget.hide()
            self.zrtp_widget.hide()
            self.zrtp_widget.stream_type = None
            notification_center = NotificationCenter()
            if old_session is not None:
                notification_center.remove_observer(self, sender=old_session)
                notification_center.remove_observer(self, sender=old_session.blink_session)
            if new_session is not None:
                notification_center.add_observer(self, sender=new_session)
                notification_center.add_observer(self, sender=new_session.blink_session)
                self._update_widgets_for_session()  # clean this up -Dan (too many functions called in 3 different places: on selection changed, here and on notifications handlers)
                self._update_control_menu()
                self._update_panel_buttons()
                self._update_session_info_panel(elements={'session', 'media', 'statistics', 'status'}, update_visibility=True)

    selected_session = property(_get_selected_session, _set_selected_session)
    del _get_selected_session, _set_selected_session

    def _update_widgets_for_session(self):
        session = self.selected_session
        widget = session.widget
        # session widget
        self.name_label.setText(widget.name_label.text())
        self.info_label.setText(widget.info_label.text())
        self.icon_label.setPixmap(widget.icon_label.pixmap())
        self.state_label.state = widget.state_label.state or 'offline'
        self.hold_icon.setVisible(widget.hold_icon.isVisibleTo(widget))
        self.composing_icon.setVisible(widget.composing_icon.isVisibleTo(widget))
        self.audio_icon.setVisible(widget.audio_icon.isVisibleTo(widget))
        self.audio_icon.setEnabled(widget.audio_icon.isEnabledTo(widget))
        self.chat_icon.setVisible(widget.chat_icon.isVisibleTo(widget))
        self.chat_icon.setEnabled(widget.chat_icon.isEnabledTo(widget))
        self.video_icon.setVisible(widget.video_icon.isVisibleTo(widget))
        self.video_icon.setEnabled(widget.video_icon.isEnabledTo(widget))
        self.screen_sharing_icon.setVisible(widget.screen_sharing_icon.isVisibleTo(widget))
        self.screen_sharing_icon.setEnabled(widget.screen_sharing_icon.isEnabledTo(widget))
        # toolbar buttons
        self.hold_button.setVisible('audio' in session.blink_session.streams)
        self.hold_button.setChecked(session.blink_session.local_hold)
        self.record_button.setVisible('audio' in session.blink_session.streams)
        self.record_button.setChecked(session.blink_session.recording)

    def _update_control_menu(self):
        menu = self.control_menu
        menu.hide()
        blink_session = self.selected_session.blink_session
        state = blink_session.state
        if state=='connecting/*' and blink_session.direction == 'outgoing' or state == 'connected/sent_proposal':
            self.control_button.setMenu(None)
            self.control_button.setIcon(self.cancel_icon)
        elif state == 'connected/received_proposal':
            self.control_button.setEnabled(False)
        else:
            self.control_button.setEnabled(True)
            self.control_button.setIcon(self.control_icon)
            menu.clear()
            if state not in ('connecting/*', 'connected/*'):
                menu.addAction(self.control_button.actions.connect)
                menu.addAction(self.control_button.actions.connect_with_audio)
                menu.addAction(self.control_button.actions.connect_with_video)
            else:
                menu.addAction(self.control_button.actions.disconnect)
                if state == 'connected':
                    stream_types = blink_session.streams.types
                    if 'audio' not in stream_types:
                        menu.addAction(self.control_button.actions.add_audio)
                    elif stream_types != {'audio'} and not stream_types.intersection({'screen-sharing', 'video'}):
                        menu.addAction(self.control_button.actions.remove_audio)
                    if 'video' not in stream_types:
                        menu.addAction(self.control_button.actions.add_video)
                    elif stream_types != {'video'}:
                        menu.addAction(self.control_button.actions.remove_video)
                    if 'screen-sharing' not in stream_types:
                        menu.addAction(self.control_button.actions.request_screen)
                        menu.addAction(self.control_button.actions.share_my_screen)
                    elif stream_types != {'screen-sharing'}:
                        menu.addAction(self.control_button.actions.end_screen_sharing)
            self.control_button.setMenu(menu)

    def _update_panel_buttons(self):
        self.info_panel_participants_button.setVisible(self.selected_session.blink_session.remote_focus)
        self.files_panel_participants_button.setVisible(self.selected_session.blink_session.remote_focus)

    def _update_session_info_panel(self, elements=set(), update_visibility=False):
        blink_session = self.selected_session.blink_session
        have_session = blink_session.state in ('connecting/*', 'connected/*', 'ending')

        if update_visibility:
            self.status_value_label.setEnabled(have_session)
            self.duration_value_label.setEnabled(have_session)
            self.account_value_label.setEnabled(have_session)
            self.remote_agent_value_label.setEnabled(have_session)
            self.audio_value_widget.setEnabled('audio' in blink_session.streams)
            self.video_value_widget.setEnabled('video' in blink_session.streams)
            self.chat_value_widget.setEnabled('chat' in blink_session.streams)
            self.screen_value_widget.setEnabled('screen-sharing' in blink_session.streams)

        session_info = blink_session.info
        audio_info = blink_session.info.streams.audio
        video_info = blink_session.info.streams.video
        chat_info = blink_session.info.streams.chat
        screen_info = blink_session.info.streams.screen_sharing
        state = "%s" % blink_session.state

        if 'status' in elements and blink_session.state in ('initialized', 'connecting/*', 'connected/*', 'ended'):
            state_map = {'initialized': 'Disconnected',
                         'connecting/dns_lookup': 'Finding destination...',
                         'connecting': 'Connecting...',
                         'connecting/ringing': 'Ringing',
                         'connecting/starting': 'Starting media...',
                         'connected': 'Connected'}

            if blink_session.state == 'ended':
                self.status_value_label.setForegroundRole(QPalette.AlternateBase if blink_session.state.error else QPalette.WindowText)
                self.status_value_label.setText(blink_session.state.reason)
            elif state in state_map:
                self.status_value_label.setForegroundRole(QPalette.WindowText)
                self.status_value_label.setText(state_map[state])

            want_duration = blink_session.state == 'connected/*' or blink_session.state == 'ended' and not blink_session.state.error
            self.status_title_label.setVisible(not want_duration)
            self.status_value_label.setVisible(not want_duration)
            self.duration_title_label.setVisible(want_duration)
            self.duration_value_label.setVisible(want_duration)

        if 'session' in elements:
            self.account_value_label.setText(blink_session.account.id)
            self.remote_agent_value_label.setText(session_info.remote_user_agent or 'N/A')

        if 'media' in elements:
            self.audio_value_label.setText(audio_info.codec or 'N/A')
            if audio_info.ice_status == 'succeeded':
                if 'relay' in {candidate.type.lower() for candidate in (audio_info.local_rtp_candidate, audio_info.remote_rtp_candidate)}:
                    self.audio_connection_label.setPixmap(self.pixmaps.relay_connection)
                    self.audio_connection_label.setToolTip('Using relay')
                else:
                    self.audio_connection_label.setPixmap(self.pixmaps.direct_connection)
                    self.audio_connection_label.setToolTip('Peer to peer')
            elif audio_info.ice_status == 'failed':
                self.audio_connection_label.setPixmap(self.pixmaps.unknown_connection)
                self.audio_connection_label.setToolTip("Couldn't negotiate ICE")
            elif audio_info.ice_status == 'disabled':
                if blink_session.contact.type == 'bonjour':
                    self.audio_connection_label.setPixmap(self.pixmaps.direct_connection)
                    self.audio_connection_label.setToolTip('Peer to peer')
                else:
                    self.audio_connection_label.setPixmap(self.pixmaps.unknown_connection)
                    self.audio_connection_label.setToolTip('ICE is disabled')
            elif audio_info.ice_status is None:
                self.audio_connection_label.setPixmap(self.pixmaps.unknown_connection)
                self.audio_connection_label.setToolTip('ICE is unavailable')
            else:
                self.audio_connection_label.setPixmap(self.pixmaps.unknown_connection)
                self.audio_connection_label.setToolTip('Negotiating ICE')

            if audio_info.encryption is not None:
                self.audio_encryption_label.setToolTip('Media is encrypted using %s (%s)' % (audio_info.encryption, audio_info.encryption_cipher))
            else:
                self.audio_encryption_label.setToolTip('Media is not encrypted')
            self._update_rtp_encryption_icon(self.audio_encryption_label)

            self.audio_connection_label.setVisible(audio_info.remote_address is not None)
            self.audio_encryption_label.setVisible(audio_info.encryption is not None)

            self.video_value_label.setText(video_info.codec or 'N/A')
            if video_info.ice_status == 'succeeded':
                if 'relay' in {candidate.type.lower() for candidate in (video_info.local_rtp_candidate, video_info.remote_rtp_candidate)}:
                    self.video_connection_label.setPixmap(self.pixmaps.relay_connection)
                    self.video_connection_label.setToolTip('Using relay')
                else:
                    self.video_connection_label.setPixmap(self.pixmaps.direct_connection)
                    self.video_connection_label.setToolTip('Peer to peer')
            elif video_info.ice_status == 'failed':
                self.video_connection_label.setPixmap(self.pixmaps.unknown_connection)
                self.video_connection_label.setToolTip("Couldn't negotiate ICE")
            elif video_info.ice_status == 'disabled':
                if blink_session.contact.type == 'bonjour':
                    self.video_connection_label.setPixmap(self.pixmaps.direct_connection)
                    self.video_connection_label.setToolTip('Peer to peer')
                else:
                    self.video_connection_label.setPixmap(self.pixmaps.unknown_connection)
                    self.video_connection_label.setToolTip('ICE is disabled')
            elif video_info.ice_status is None:
                self.video_connection_label.setPixmap(self.pixmaps.unknown_connection)
                self.video_connection_label.setToolTip('ICE is unavailable')
            else:
                self.video_connection_label.setPixmap(self.pixmaps.unknown_connection)
                self.video_connection_label.setToolTip('Negotiating ICE')

            if video_info.encryption is not None:
                self.video_encryption_label.setToolTip('Media is encrypted using %s (%s)' % (video_info.encryption, video_info.encryption_cipher))
            else:
                self.video_encryption_label.setToolTip('Media is not encrypted')
            self._update_rtp_encryption_icon(self.video_encryption_label)

            self.video_connection_label.setVisible(video_info.remote_address is not None)
            self.video_encryption_label.setVisible(video_info.encryption is not None)

            if self.zrtp_widget.isVisibleTo(self.info_panel):
                # refresh the ZRTP widget (we need to hide/change/show because in certain configurations it flickers when changed while visible)
                stream_info = blink_session.info.streams[self.zrtp_widget.stream_type]
                self.zrtp_widget.hide()
                self.zrtp_widget.peer_name = stream_info.zrtp_peer_name
                self.zrtp_widget.peer_verified = stream_info.zrtp_verified
                self.zrtp_widget.sas = stream_info.zrtp_sas
                self.zrtp_widget.show()

            if any(len(path) > 1 for path in (chat_info.full_local_path, chat_info.full_remote_path)):
                self.chat_value_label.setText('Using relay')
                self.chat_connection_label.setPixmap(self.pixmaps.relay_connection)
                self.chat_connection_label.setToolTip('Using relay')
            elif chat_info.full_local_path and chat_info.full_remote_path:
                self.chat_value_label.setText('Peer to peer')
                self.chat_connection_label.setPixmap(self.pixmaps.direct_connection)
                self.chat_connection_label.setToolTip('Peer to peer')
            else:
                self.chat_value_label.setText('N/A')

            if chat_info.encryption is not None and chat_info.transport == 'tls':
                self.chat_encryption_label.setToolTip('Media is encrypted using TLS and {0.encryption} ({0.encryption_cipher})'.format(chat_info))
            elif chat_info.encryption is not None:
                self.chat_encryption_label.setToolTip('Media is encrypted using {0.encryption} ({0.encryption_cipher})'.format(chat_info))
            elif chat_info.transport == 'tls':
                self.chat_encryption_label.setToolTip('Media is encrypted using TLS')
            else:
                self.chat_encryption_label.setToolTip('Media is not encrypted')
            self._update_chat_encryption_icon()

            self.chat_connection_label.setVisible(chat_info.remote_address is not None)
            self.chat_encryption_label.setVisible(chat_info.remote_address is not None and (chat_info.encryption is not None or chat_info.transport == 'tls'))

            if self.otr_widget.isVisibleTo(self.info_panel):
                # refresh the OTR widget (we need to hide/change/show because in certain configurations it flickers when changed while visible)
                stream_info = blink_session.info.streams.chat
                self.otr_widget.hide()
                self.otr_widget.peer_name = stream_info.otr_peer_name
                self.otr_widget.peer_verified = stream_info.otr_verified
                self.otr_widget.peer_fingerprint = stream_info.otr_peer_fingerprint
                self.otr_widget.my_fingerprint = stream_info.otr_key_fingerprint
                self.otr_widget.smp_status = stream_info.smp_status
                self.otr_widget.show()

            if screen_info.remote_address is not None and screen_info.mode == 'active':
                self.screen_value_label.setText('Viewing remote')
            elif screen_info.remote_address is not None and screen_info.mode == 'passive':
                self.screen_value_label.setText('Sharing local')
            else:
                self.screen_value_label.setText('N/A')

            if any(len(path) > 1 for path in (screen_info.full_local_path, screen_info.full_remote_path)):
                self.screen_connection_label.setPixmap(self.pixmaps.relay_connection)
                self.screen_connection_label.setToolTip('Using relay')
            elif screen_info.full_local_path and screen_info.full_remote_path:
                self.screen_connection_label.setPixmap(self.pixmaps.direct_connection)
                self.screen_connection_label.setToolTip('Peer to peer')

            self.screen_encryption_label.setToolTip('Media is encrypted using TLS')

            self.screen_connection_label.setVisible(screen_info.remote_address is not None)
            self.screen_encryption_label.setVisible(screen_info.remote_address is not None and screen_info.transport == 'tls')

        if 'statistics' in elements:
            self.duration_value_label.value = session_info.duration
            self.audio_latency_graph.data = audio_info.latency
            self.video_latency_graph.data = video_info.latency
            self.audio_packet_loss_graph.data = audio_info.packet_loss
            self.video_packet_loss_graph.data = video_info.packet_loss
            self.incoming_traffic_graph.data = audio_info.incoming_traffic
            self.outgoing_traffic_graph.data = audio_info.outgoing_traffic
            self.latency_graph.update()
            self.packet_loss_graph.update()
            self.traffic_graph.update()

    def _update_rtp_encryption_icon(self, encryption_label):
        stream = self.selected_session.blink_session.streams.get(encryption_label.stream_type)
        stream_info = self.selected_session.blink_session.info.streams[encryption_label.stream_type]
        if encryption_label.isEnabled() and stream_info.encryption == 'ZRTP':
            if encryption_label.hovered and stream is not None and not stream._done:
                encryption_label.setPixmap(self.pixmaps.light_green_lock if stream_info.zrtp_verified else self.pixmaps.light_orange_lock)
            else:
                encryption_label.setPixmap(self.pixmaps.green_lock if stream_info.zrtp_verified else self.pixmaps.orange_lock)
        else:
            encryption_label.setPixmap(self.pixmaps.grey_lock)

    def _update_chat_encryption_icon(self):
        stream = self.selected_session.chat_stream
        stream_info = self.selected_session.blink_session.info.streams.chat
        if self.chat_encryption_label.isEnabled() and stream_info.encryption == 'OTR':
            if self.chat_encryption_label.hovered and stream is not None and not stream._done:
                self.chat_encryption_label.setPixmap(self.pixmaps.light_green_lock if stream_info.otr_verified else self.pixmaps.light_orange_lock)
            else:
                self.chat_encryption_label.setPixmap(self.pixmaps.green_lock if stream_info.otr_verified else self.pixmaps.orange_lock)
        else:
            self.chat_encryption_label.setPixmap(self.pixmaps.grey_lock)

    def show(self):
        super(ChatWindow, self).show()
        self.raise_()
        self.activateWindow()

    def closeEvent(self, event):
        QSettings().setValue("chat_window/geometry", self.saveGeometry())
        super(ChatWindow, self).closeEvent(event)

    def eventFilter(self, watched, event):
        event_type = event.type()
        if watched is self.session_widget:
            if event_type == QEvent.HoverEnter:
                watched.hovered = True
            elif event_type == QEvent.HoverLeave:
                watched.hovered = False
            elif event_type == QEvent.MouseButtonDblClick and event.button() == Qt.LeftButton:
                self._EH_ShowSessions()
        elif watched is self.state_label:
            if event_type == QEvent.MouseButtonRelease and event.button() == Qt.LeftButton and event.modifiers() == Qt.NoModifier:
                upper_half = QRect(0, 0, self.state_label.width(), self.state_label.height()/2)
                if upper_half.contains(event.pos()):
                    self._EH_CloseSession()
                else:
                    self._EH_ShowSessions()
            elif event_type == QEvent.Paint:  # and self.session_widget.hovered:
                watched.event(event)
                self.drawSessionWidgetIndicators()
                return True
        elif watched in (self.latency_graph, self.packet_loss_graph, self.traffic_graph):
            if event_type == QEvent.Wheel and event.modifiers() == Qt.ControlModifier:
                settings = BlinkSettings()
                wheel_delta = event.angleDelta().y()
                if wheel_delta > 0 and settings.chat_window.session_info.graph_time_scale > GraphTimeScale.min_value:
                    settings.chat_window.session_info.graph_time_scale -= 1
                    settings.save()
                elif wheel_delta < 0 and settings.chat_window.session_info.graph_time_scale < GraphTimeScale.max_value:
                    settings.chat_window.session_info.graph_time_scale += 1
                    settings.save()
        elif watched in (self.audio_encryption_label, self.video_encryption_label):
            if event_type == QEvent.Enter:
                watched.hovered = True
                self._update_rtp_encryption_icon(watched)
            elif event_type == QEvent.Leave:
                watched.hovered = False
                self._update_rtp_encryption_icon(watched)
            elif event_type == QEvent.EnabledChange and not watched.isEnabled():
                watched.setPixmap(self.pixmaps.grey_lock)
            elif event_type in (QEvent.MouseButtonPress, QEvent.MouseButtonDblClick) and event.button() == Qt.LeftButton and event.modifiers() == Qt.NoModifier and watched.isEnabled():
                self._EH_RTPEncryptionLabelClicked(watched)
        elif watched is self.chat_encryption_label:
            if event_type == QEvent.Enter:
                watched.hovered = True
                self._update_chat_encryption_icon()
            elif event_type == QEvent.Leave:
                watched.hovered = False
                self._update_chat_encryption_icon()
            elif event_type == QEvent.EnabledChange and not watched.isEnabled():
                watched.setPixmap(self.pixmaps.grey_lock)
            elif event_type in (QEvent.MouseButtonPress, QEvent.MouseButtonDblClick) and event.button() == Qt.LeftButton and event.modifiers() == Qt.NoModifier and watched.isEnabled():
                self._EH_ChatEncryptionLabelClicked()
        elif watched is self.info_panel:
            if event_type == QEvent.Resize:
                if self.zrtp_widget.isVisibleTo(self.info_panel):
                    rect = self.zrtp_widget.geometry()
                    rect.setWidth(self.info_panel.width())
                    self.zrtp_widget.setGeometry(rect)
                if self.otr_widget.isVisibleTo(self.info_panel):
                    rect = self.otr_widget.geometry()
                    rect.setWidth(self.info_panel.width())
                    self.otr_widget.setGeometry(rect)
        return False

    def drawSessionWidgetIndicators(self):
        painter = QPainter(self.state_label)
        palette = self.state_label.palette()
        rect = self.state_label.rect()

        pen_thickness = 1.6

        if self.state_label.state is not None:
            background_color = self.state_label.state_colors[self.state_label.state]
            base_contrast_color = self.calc_light_color(background_color)
            gradient = QLinearGradient(0, 0, 1, 0)
            gradient.setCoordinateMode(QLinearGradient.ObjectBoundingMode)
            gradient.setColorAt(0.0, self.color_with_alpha(base_contrast_color, 0.3*255))
            gradient.setColorAt(1.0, self.color_with_alpha(base_contrast_color, 0.8*255))
            contrast_color = QBrush(gradient)
        else:
            background_color = palette.color(QPalette.Window)
            contrast_color = self.calc_light_color(background_color)
        foreground_color = palette.color(QPalette.Normal, QPalette.WindowText)
        line_color = self.deco_color(background_color, foreground_color)

        pen = QPen(line_color, pen_thickness, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin)
        contrast_pen = QPen(contrast_color, pen_thickness, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin)

        # draw the expansion indicator at the bottom (works best with a state_label of width 14)
        arrow_rect = QRect(0, 0, 14, 14)
        arrow_rect.moveBottomRight(rect.bottomRight())

        arrow = QPolygonF([QPointF(-3, -1.5), QPointF(0.5, 2.5), QPointF(4, -1.5)])
        arrow.translate(1, 1)

        painter.save()
        painter.setRenderHint(QPainter.Antialiasing, True)
        painter.setCompositionMode(QPainter.CompositionMode_SourceOver)
        painter.translate(arrow_rect.center())
        painter.translate(0, +1)
        painter.setPen(contrast_pen)
        painter.drawPolyline(arrow)
        painter.translate(0, -1)
        painter.setPen(pen)
        painter.drawPolyline(arrow)
        painter.restore()

        # draw the close indicator at the top (works best with a state_label of width 14)
        cross_rect = QRect(0, 0, 14, 14)
        cross_rect.moveTopRight(rect.topRight())

        painter.save()
        painter.setRenderHint(QPainter.Antialiasing, True)
        painter.setCompositionMode(QPainter.CompositionMode_SourceOver)
        painter.translate(cross_rect.center())
        painter.translate(+1.5, +1)
        painter.translate(0, +1)
        painter.setPen(contrast_pen)
        painter.drawLine(-3.5, -3.5, 3.5, 3.5)
        painter.drawLine(-3.5, 3.5, 3.5, -3.5)
        painter.translate(0, -1)
        painter.setPen(pen)
        painter.drawLine(-3.5, -3.5, 3.5, 3.5)
        painter.drawLine(-3.5, 3.5, 3.5, -3.5)
        painter.restore()

    @run_in_gui_thread
    def handle_notification(self, notification):
        handler = getattr(self, '_NH_%s' % notification.name, Null)
        handler(notification)

    def _NH_SIPApplicationDidStart(self, notification):
        notification.center.add_observer(self, name='CFGSettingsObjectDidChange')

        blink_settings = BlinkSettings()
        if blink_settings.chat_window.session_info.alternate_style:
            title_role = 'alt-title'
            value_role = 'alt-value'
        else:
            title_role = 'title'
            value_role = 'value'
        for label in (attr for name, attr in vars(self).items() if name.endswith('_title_label') and attr.property('role') is not None):
            label.setProperty('role', title_role)
        for label in (attr for name, attr in vars(self).items() if name.endswith('_value_label') or name.endswith('_value_widget') and attr.property('role') is not None):
            label.setProperty('role', value_role)
        self.info_panel_container_widget.setStyleSheet(self.info_panel_container_widget.styleSheet())
        self.latency_graph.horizontalPixelsPerUnit = blink_settings.chat_window.session_info.graph_time_scale
        self.packet_loss_graph.horizontalPixelsPerUnit = blink_settings.chat_window.session_info.graph_time_scale
        self.traffic_graph.horizontalPixelsPerUnit = blink_settings.chat_window.session_info.graph_time_scale
        self.latency_graph.update()
        self.packet_loss_graph.update()
        self.traffic_graph.update()

        self.dummy_tab = ChatWidget(None, self.tab_widget)
        self.dummy_tab.setDisabled(True)
        self.tab_widget.addTab(self.dummy_tab, "Dummy")
        self.tab_widget.setCurrentWidget(self.dummy_tab)

    def _NH_CFGSettingsObjectDidChange(self, notification):
        settings = SIPSimpleSettings()
        blink_settings = BlinkSettings()
        if notification.sender is settings:
            if 'audio.muted' in notification.data.modified:
                self.mute_button.setChecked(settings.audio.muted)
        elif notification.sender is blink_settings:
            if 'presence.icon' in notification.data.modified:
                QWebSettings.clearMemoryCaches()
            if 'chat_window.session_info.alternate_style' in notification.data.modified:
                if blink_settings.chat_window.session_info.alternate_style:
                    title_role = 'alt-title'
                    value_role = 'alt-value'
                else:
                    title_role = 'title'
                    value_role = 'value'
                for label in (attr for name, attr in vars(self).items() if name.endswith('_title_label') and attr.property('role') is not None):
                    label.setProperty('role', title_role)
                for label in (attr for name, attr in vars(self).items() if name.endswith('_value_label') or name.endswith('_value_widget') and attr.property('role') is not None):
                    label.setProperty('role', value_role)
                self.info_panel_container_widget.setStyleSheet(self.info_panel_container_widget.styleSheet())
            if 'chat_window.session_info.bytes_per_second' in notification.data.modified:
                self.traffic_graph.update()
            if 'chat_window.session_info.graph_time_scale' in notification.data.modified:
                self.latency_graph.horizontalPixelsPerUnit = blink_settings.chat_window.session_info.graph_time_scale
                self.packet_loss_graph.horizontalPixelsPerUnit = blink_settings.chat_window.session_info.graph_time_scale
                self.traffic_graph.horizontalPixelsPerUnit = blink_settings.chat_window.session_info.graph_time_scale
                self.latency_graph.update()
                self.packet_loss_graph.update()
                self.traffic_graph.update()

    def _NH_BlinkSessionNewIncoming(self, notification):
        if notification.sender.streams.types.intersection(self.__streamtypes__):
            self.show()

    def _NH_BlinkSessionNewOutgoing(self, notification):
        if notification.sender.stream_descriptions.types.intersection(self.__streamtypes__):
            self.show()

    def _NH_BlinkSessionDidReinitializeForIncoming(self, notification):
        model = self.session_model
        position = model.sessions.index(notification.sender.items.chat)
        selection_model = self.session_list.selectionModel()
        selection_model.select(model.index(position), selection_model.ClearAndSelect)
        self.session_list.scrollTo(model.index(position), QListView.EnsureVisible)  # or PositionAtCenter
        if notification.sender.streams.types.intersection(self.__streamtypes__):
            self.show()

    def _NH_BlinkSessionDidReinitializeForOutgoing(self, notification):
        model = self.session_model
        position = model.sessions.index(notification.sender.items.chat)
        selection_model = self.session_list.selectionModel()
        selection_model.select(model.index(position), selection_model.ClearAndSelect)
        self.session_list.scrollTo(model.index(position), QListView.EnsureVisible)  # or PositionAtCenter
        if notification.sender.stream_descriptions.types.intersection(self.__streamtypes__):
            self.show()

    # use BlinkSessionNewIncoming/Outgoing to show the chat window if there is a chat stream available (like with reinitialize) instead of using the sessionAdded signal from the model -Dan
    # or maybe not. sessionAdded means it was added to the model, while during NewIncoming/Outgoing we do not know that yet. but then we have a problem with the DidReinitialize since
    # they do not check if the session is in the model. maybe the right approach is to always have BlinkSessions in the model and if we need any other kind of sessions we create a
    # different class for them that posts different notifications. in that case we can do in in NewIncoming/Outgoing -Dan

    def _NH_BlinkSessionWillAddStream(self, notification):
        if notification.data.stream.type in self.__streamtypes__:
            self.show()

    def _NH_BlinkSessionDidRemoveStream(self, notification):
        self._update_control_menu()
        self._update_session_info_panel(update_visibility=True)

    def _NH_BlinkSessionDidChangeState(self, notification):
        # even if we use this, we also need to listen for BlinkSessionDidRemoveStream as that transition doesn't change the state at all -Dan
        self._update_control_menu()
        self._update_panel_buttons()
        self._update_session_info_panel(elements={'status'}, update_visibility=True)

    def _NH_BlinkSessionDidEnd(self, notification):
        if self.selected_session.active_panel is not self.info_panel:
            if self.sliding_panels:
                self.session_details.slideInWidget(self.info_panel, direction=self.slide_direction)
            else:
                self.session_details.setCurrentWidget(self.info_panel)
            self.selected_session.active_panel = self.info_panel

    def _NH_BlinkSessionInfoUpdated(self, notification):
        self._update_session_info_panel(elements=notification.data.elements)

    def _NH_BlinkSessionWillAddParticipant(self, notification):
        if len(notification.sender.server_conference.participants) == 1 and self.selected_session.active_panel is not self.participants_panel:
            if self.sliding_panels:
                self.session_details.slideInWidget(self.participants_panel, direction=self.slide_direction)
            else:
                self.session_details.setCurrentWidget(self.participants_panel)
            self.selected_session.active_panel = self.participants_panel

    def _NH_ChatSessionItemDidChange(self, notification):
        self._update_widgets_for_session()

    def _NH_ChatStreamGotMessage(self, notification):
        blink_session = notification.sender.blink_session
        session = blink_session.items.chat

        if session is None:
            return

        message = notification.data.message

        if message.content_type.startswith('image/'):
            content = '''<img src="data:{};base64,{}" class="scaled-to-fit" />'''.format(message.content_type, message.content.decode('base64').rstrip())
        elif message.content_type.startswith('text/'):
            content = message.content
            content = HtmlProcessor.autolink(content if message.content_type == 'text/html' else QTextDocument(content).toHtml())
        else:
            return

        uri = '%s@%s' % (message.sender.uri.user.decode(), message.sender.uri.host.decode())
        account_manager = AccountManager()
        if account_manager.has_account(uri):
            account = account_manager.get_account(uri)
            sender = ChatSender(message.sender.display_name or account.display_name, uri, session.chat_widget.user_icon.filename)
        elif blink_session.remote_focus:
            contact, contact_uri = URIUtils.find_contact(uri)
            sender = ChatSender(message.sender.display_name or contact.name, uri, contact.icon.filename)
        else:
            sender = ChatSender(message.sender.display_name or session.name, uri, session.icon.filename)

        is_status_message = any(h.name == 'Message-Type' and h.value == 'status' and h.namespace == 'urn:ag-projects:xml:ns:cpim' for h in message.additional_headers)
        if is_status_message:
            session.chat_widget.add_message(ChatStatus(content))
        else:
            session.chat_widget.add_message(ChatMessage(content, sender, 'incoming'))

        session.remote_composing = False
        settings = SIPSimpleSettings()
        if settings.sounds.play_message_alerts and self.selected_session is session:
            player = WavePlayer(SIPApplication.alert_audio_bridge.mixer, Resources.get('sounds/message_received.wav'), volume=20)
            SIPApplication.alert_audio_bridge.add(player)
            player.start()

    def _NH_ChatStreamGotComposingIndication(self, notification):
        session = notification.sender.blink_session.items.chat
        if session is None:
            return
        session.update_composing_indication(notification.data)

    def _NH_ChatStreamDidSendMessage(self, notification):
        session = notification.sender.blink_session.items.chat
        if session is None:
            return
        # TODO: do we want to use this? Play the message sent tone? -Saul

    def _NH_ChatStreamDidDeliverMessage(self, notification):
        session = notification.sender.blink_session.items.chat
        if session is None:
            return
        # TODO: implement -Saul

    def _NH_ChatStreamDidNotDeliverMessage(self, notification):
        session = notification.sender.blink_session.items.chat
        if session is None:
            return
        # TODO: implement -Saul

    def _NH_ChatStreamOTREncryptionStateChanged(self, notification):
        session = notification.sender.blink_session.items.chat
        if session is None:
            return
        if notification.data.new_state is OTRState.Encrypted:
            session.chat_widget.add_message(ChatStatus('Encryption enabled'))
        elif notification.data.old_state is OTRState.Encrypted:
            session.chat_widget.add_message(ChatStatus('Encryption disabled'))
            self.otr_widget.hide()
        if notification.data.new_state is OTRState.Finished:
            session.chat_widget.chat_input.lock(EncryptionLock)
            # todo: play sound here?

    def _NH_ChatStreamOTRError(self, notification):
        session = notification.sender.blink_session.items.chat
        if session is not None:
            message = "OTR Error: {.error}".format(notification.data)
            session.chat_widget.add_message(ChatStatus(message))

    def _NH_MediaStreamDidInitialize(self, notification):
        if notification.sender.type != 'chat':
            return
        session = notification.sender.blink_session.items.chat
        if session is None:
            return
        # session.chat_widget.add_message(ChatStatus('Connecting...'))  # disable it until we can replace it in the DOM -Dan

    def _NH_MediaStreamDidNotInitialize(self, notification):
        if notification.sender.type != 'chat':
            return
        session = notification.sender.blink_session.items.chat
        if session is None:
            return
        session.chat_widget.add_message(ChatStatus('Failed to initialize chat: %s' % notification.data.reason))

    def _NH_MediaStreamDidStart(self, notification):
        if notification.sender.type != 'chat':
            return
        session = notification.sender.blink_session.items.chat
        if session is None:
            return
        session.chat_widget.add_message(ChatStatus('Connected'))

    def _NH_MediaStreamDidEnd(self, notification):
        if notification.sender.type != 'chat':
            return
        session = notification.sender.blink_session.items.chat
        if session is None:
            return
        if notification.data.error is not None:
            session.chat_widget.add_message(ChatStatus('Disconnected: %s' % notification.data.error))
        else:
            session.chat_widget.add_message(ChatStatus('Disconnected'))

    def _NH_MediaStreamWillEnd(self, notification):
        stream = notification.sender
        if stream.type == 'chat' and stream.blink_session.items.chat is self.selected_session:
            self.otr_widget.hide()
        if stream.type == self.zrtp_widget.stream_type and stream.blink_session.items.chat is self.selected_session:
            self.zrtp_widget.hide()
            self.zrtp_widget.stream_type = None

    # signal handlers
    #
    def _SH_InfoButtonClicked(self, checked):
        if self.sliding_panels:
            self.session_details.slideInWidget(self.info_panel, direction=self.slide_direction)
        else:
            self.session_details.setCurrentWidget(self.info_panel)
        self.selected_session.active_panel = self.info_panel

    def _SH_FilesButtonClicked(self, checked):
        if self.sliding_panels:
            self.session_details.slideInWidget(self.files_panel, direction=self.slide_direction)
        else:
            self.session_details.setCurrentWidget(self.files_panel)
        self.selected_session.active_panel = self.files_panel

    def _SH_ParticipantsButtonClicked(self, checked):
        if self.sliding_panels:
            self.session_details.slideInWidget(self.participants_panel, direction=self.slide_direction)
        else:
            self.session_details.setCurrentWidget(self.participants_panel)
        self.selected_session.active_panel = self.participants_panel

    def _SH_LatencyGraphUpdated(self):
        self.latency_label.setText('Network Latency: %dms, max=%dms' % (max(self.audio_latency_graph.last_value, self.video_latency_graph.last_value), self.latency_graph.max_value))

    def _SH_PacketLossGraphUpdated(self):
        self.packet_loss_label.setText('Packet Loss: %.1f%%, max=%.1f%%' % (max(self.audio_packet_loss_graph.last_value, self.video_packet_loss_graph.last_value), self.packet_loss_graph.max_value))

    def _SH_TrafficGraphUpdated(self):
        blink_settings = BlinkSettings()
        if blink_settings.chat_window.session_info.bytes_per_second:
            incoming_traffic = TrafficNormalizer.normalize(self.incoming_traffic_graph.last_value)
            outgoing_traffic = TrafficNormalizer.normalize(self.outgoing_traffic_graph.last_value)
        else:
            incoming_traffic = TrafficNormalizer.normalize(self.incoming_traffic_graph.last_value*8, bits_per_second=True)
            outgoing_traffic = TrafficNormalizer.normalize(self.outgoing_traffic_graph.last_value*8, bits_per_second=True)
        self.traffic_label.setText("""<p>Traffic: <span style="font-family: sans-serif; color: #d70000;">\u2193</span> %s <span style="font-family: sans-serif; color: #0064d7;">\u2191</span> %s</p>""" % (incoming_traffic, outgoing_traffic))

    def _SH_MuteButtonClicked(self, checked):
        settings = SIPSimpleSettings()
        settings.audio.muted = checked
        settings.save()

    def _SH_HoldButtonClicked(self, checked):
        if checked:
            self.selected_session.blink_session.hold()
        else:
            self.selected_session.blink_session.unhold()

    def _SH_RecordButtonClicked(self, checked):
        if checked:
            self.selected_session.blink_session.start_recording()
        else:
            self.selected_session.blink_session.stop_recording()

    def _SH_ControlButtonClicked(self):
        # this is only called if the control button doesn't have a menu attached
        if self.selected_session.blink_session.state == 'connected/sent_proposal':
            self.selected_session.blink_session.sip_session.cancel_proposal()
        else:
            self.selected_session.end()

    def _SH_SessionModelSessionAdded(self, session):
        model = self.session_model
        position = model.sessions.index(session)
        session.chat_widget = ChatWidget(session, self.tab_widget)
        session.video_widget = VideoWidget(session, session.chat_widget)
        session.active_panel = self.info_panel
        self.tab_widget.insertTab(position, session.chat_widget, session.name)
        self.no_sessions_label.hide()
        selection_model = self.session_list.selectionModel()
        selection_model.select(model.index(position), selection_model.ClearAndSelect)
        self.session_list.scrollTo(model.index(position), QListView.EnsureVisible) # or PositionAtCenter
        session.chat_widget.chat_input.setFocus(Qt.OtherFocusReason)

    def _SH_SessionModelSessionRemoved(self, session):
        self.tab_widget.removeTab(self.tab_widget.indexOf(session.chat_widget))
        session.chat_widget = None
        session.video_widget = None
        session.active_panel = None
        if not self.session_model.sessions:
            self.close()
            self.no_sessions_label.show()
        elif not self.session_list.isVisibleTo(self):
            if self.session_list.animation:
                self.session_list.animation.setDirection(QPropertyAnimation.Forward)
                self.session_list.animation.setStartValue(self.session_widget.geometry())
                self.session_list.animation.setEndValue(self.session_panel.rect())
            self.session_list.show()
            self.session_list.animation.start()

    def _SH_SessionModelSessionAboutToBeRemoved(self, session):
        # choose another one to select (a chat only or ended session if available, else one with audio but keep audio on hold? or select nothing and display the dummy tab?)
        # selection_model = self.session_list.selectionModel()
        # selection_model.clearSelection()
        pass

    def _SH_SessionListSelectionChanged(self, selected, deselected):
        # print "-- chat selection changed %s -> %s" % ([x.row() for x in deselected.indexes()], [x.row() for x in selected.indexes()])
        self.selected_session = selected[0].topLeft().data(Qt.UserRole) if selected else None
        if self.selected_session is not None:
            self.tab_widget.setCurrentWidget(self.selected_session.chat_widget)  # why do we switch the tab here, but do everything else in the selected_session property setter? -Dan
            self.session_details.setCurrentWidget(self.selected_session.active_panel)
            self.participants_list.setModel(self.selected_session.participants_model)
            self.control_button.setEnabled(True)
        else:
            self.tab_widget.setCurrentWidget(self.dummy_tab)
            self.session_details.setCurrentWidget(self.info_panel)
            self.participants_list.setModel(None)
            self.control_button.setEnabled(False)

    def _SH_OTRWidgetNameChanged(self):
        stream = self.selected_session.chat_stream or Null
        stream.encryption.peer_name = self.otr_widget.peer_name

    def _SH_OTRWidgetStatusChanged(self):
        stream = self.selected_session.chat_stream or Null
        stream.encryption.verified = self.otr_widget.peer_verified

    def _SH_ZRTPWidgetNameChanged(self):
        stream = self.selected_session.blink_session.streams.get(self.zrtp_widget.stream_type, Null)
        stream.encryption.zrtp.peer_name = self.zrtp_widget.peer_name

    def _SH_ZRTPWidgetStatusChanged(self):
        stream = self.selected_session.blink_session.streams.get(self.zrtp_widget.stream_type, Null)
        stream.encryption.zrtp.verified = self.zrtp_widget.peer_verified

    def _AH_Connect(self):
        blink_session = self.selected_session.blink_session
        blink_session.init_outgoing(blink_session.account, blink_session.contact, blink_session.contact_uri, stream_descriptions=[StreamDescription('chat')], reinitialize=True)
        blink_session.connect()

    def _AH_ConnectWithAudio(self):
        stream_descriptions = [StreamDescription('audio'), StreamDescription('chat')]
        blink_session = self.selected_session.blink_session
        blink_session.init_outgoing(blink_session.account, blink_session.contact, blink_session.contact_uri, stream_descriptions=stream_descriptions, reinitialize=True)
        blink_session.connect()

    def _AH_ConnectWithVideo(self):
        stream_descriptions = [StreamDescription('audio'), StreamDescription('video'), StreamDescription('chat')]
        blink_session = self.selected_session.blink_session
        blink_session.init_outgoing(blink_session.account, blink_session.contact, blink_session.contact_uri, stream_descriptions=stream_descriptions, reinitialize=True)
        blink_session.connect()

    def _AH_Disconnect(self):
        self.selected_session.end()

    def _AH_AddAudio(self):
        self.selected_session.blink_session.add_stream(StreamDescription('audio'))

    def _AH_RemoveAudio(self):
        self.selected_session.blink_session.remove_stream(self.selected_session.blink_session.streams.get('audio'))

    def _AH_AddVideo(self):
        if 'audio' in self.selected_session.blink_session.streams:
            self.selected_session.blink_session.add_stream(StreamDescription('video'))
        else:
            self.selected_session.blink_session.add_streams([StreamDescription('video'), StreamDescription('audio')])

    def _AH_RemoveVideo(self):
        self.selected_session.blink_session.remove_stream(self.selected_session.blink_session.streams.get('video'))

    def _AH_RequestScreen(self):
        if 'audio' in self.selected_session.blink_session.streams:
            self.selected_session.blink_session.add_stream(StreamDescription('screen-sharing', mode='viewer'))
        else:
            self.selected_session.blink_session.add_streams([StreamDescription('screen-sharing', mode='viewer'), StreamDescription('audio')])

    def _AH_ShareMyScreen(self):
        if 'audio' in self.selected_session.blink_session.streams:
            self.selected_session.blink_session.add_stream(StreamDescription('screen-sharing', mode='server'))
        else:
            self.selected_session.blink_session.add_streams([StreamDescription('screen-sharing', mode='server'), StreamDescription('audio')])

    def _AH_EndScreenSharing(self):
        self.selected_session.blink_session.remove_stream(self.selected_session.blink_session.streams.get('screen-sharing'))

    def _AH_MainWindow(self):
        blink = QApplication.instance()
        blink.main_window.show()

    def _EH_CloseSession(self):
        if self.selected_session is not None:
            self.selected_session.end(delete=True)

    def _EH_ShowSessions(self):
        self.session_list.animation.setDirection(QPropertyAnimation.Forward)
        self.session_list.animation.setStartValue(self.session_widget.geometry())
        self.session_list.animation.setEndValue(self.session_panel.rect())
        self.session_list.scrollToTop()
        self.session_list.show()
        self.session_list.animation.start()

    def _EH_ChatEncryptionLabelClicked(self):
        stream = self.selected_session.chat_stream
        stream_info = self.selected_session.blink_session.info.streams.chat
        if stream is not None and not stream._done and stream_info.encryption == 'OTR':
            if self.otr_widget.isVisible():
                self.otr_widget.hide()
            else:
                encryption_label = self.chat_encryption_label
                self.zrtp_widget.hide()
                self.otr_widget.peer_name = stream_info.otr_peer_name
                self.otr_widget.peer_verified = stream_info.otr_verified
                self.otr_widget.peer_fingerprint = stream_info.otr_peer_fingerprint
                self.otr_widget.my_fingerprint = stream_info.otr_key_fingerprint
                self.otr_widget.smp_status = stream_info.smp_status
                self.otr_widget.setGeometry(QRect(0, encryption_label.rect().translated(encryption_label.mapTo(self.info_panel, QPoint(0, 0))).bottom() + 3, self.info_panel.width(), 320))
                self.otr_widget.verification_stack.setCurrentWidget(self.otr_widget.smp_panel)
                self.otr_widget.show()
                self.otr_widget.peer_name_value.setFocus(Qt.OtherFocusReason)

    def _EH_RTPEncryptionLabelClicked(self, encryption_label):
        stream = self.selected_session.blink_session.streams.get(encryption_label.stream_type)
        stream_info = self.selected_session.blink_session.info.streams[encryption_label.stream_type]
        if stream is not None and not stream._done and stream_info.encryption == 'ZRTP':
            if self.zrtp_widget.isVisible() and self.zrtp_widget.stream_type == encryption_label.stream_type:
                self.zrtp_widget.hide()
                self.zrtp_widget.stream_type = None
            else:
                self.zrtp_widget.hide()
                self.zrtp_widget.peer_name = stream_info.zrtp_peer_name
                self.zrtp_widget.peer_verified = stream_info.zrtp_verified
                self.zrtp_widget.sas = stream_info.zrtp_sas
                self.zrtp_widget.stream_type = encryption_label.stream_type
                self.zrtp_widget.setGeometry(QRect(0, encryption_label.rect().translated(encryption_label.mapTo(self.info_panel, QPoint(0, 0))).bottom() + 3, self.info_panel.width(), 320))
                self.zrtp_widget.show()
                self.zrtp_widget.peer_name_value.setFocus(Qt.OtherFocusReason)

del ui_class, base_class


# Helpers
#

class HtmlProcessor(object):
    _autolink_re = [re.compile(r"""
                                (?P<body>
                                  https?://(?:[^:@/]+(?::[^@]*)?@)?(?P<host>[a-z0-9.-]+)(?::\d*)?    # scheme :// [ user [ : password ] @ ] host [ : port ]
                                  (?:/(?:[\w/%!$@#*&='~:;,.+-]*(?:\([\w/%!$@#*&='~:;,.+-]*\))?)*)?   # [ / path]
                                  (?:\?(?:[\w/%!$@#*&='~:;,.+-]*(?:\([\w/%!$@#*&='~:;,.+-]*\))?)*)?  # [ ? query]
                                )
                                """, re.IGNORECASE | re.UNICODE | re.VERBOSE),
                    re.compile(r"""
                                (?P<body>
                                  ftps?://(?:[^:@/]+(?::[^@]*)?@)?(?P<host>[a-z0-9.-]+)(?::\d*)?                  # scheme :// [ user [ : password ] @ ] host [ : port ]
                                  (?:/(?:[\w/%!?$@*&='~:,.+-]*(?:\([\w/%!?$@*&='~:,.+-]*\))?)*(?:;type=[aid])?)?  # [ / path [ ;type=a/i/d ] ]
                                )
                                """, re.IGNORECASE | re.UNICODE | re.VERBOSE),
                    re.compile(r'mailto:(?P<body>[\w.-]+@(?P<host>[a-z0-9.-]+))', re.IGNORECASE | re.UNICODE)]

    @classmethod
    def autolink(cls, content):
        if isinstance(content, str):
            doc = html.fromstring(content)
            autolink(doc, link_regexes=cls._autolink_re)
            return html.tostring(doc, encoding='unicode')  # add method='xml' to get <br/> xhtml style tags and doctype=doc.getroottree().docinfo.doctype for prepending the DOCTYPE line
        else:
            autolink(content, link_regexes=cls._autolink_re)
            return content

    @classmethod
    def normalize(cls, content):
        return content


class TrafficNormalizer(object):
    boundaries = [(             1024, '%d%ss',                   1),
                  (          10*1024, '%.2fk%ss',           1024.0),  (        1024*1024, '%.1fk%ss',           1024.0),
                  (     10*1024*1024, '%.2fM%ss',      1024*1024.0),  (   1024*1024*1024, '%.1fM%ss',      1024*1024.0),
                  (10*1024*1024*1024, '%.2fG%ss', 1024*1024*1024.0),  (float('infinity'), '%.1fG%ss', 1024*1024*1024.0)]

    @classmethod
    def normalize(cls, value, bits_per_second=False):
        for boundary, format, divisor in cls.boundaries:
            if value < boundary:
                return format % (value/divisor, 'bp' if bits_per_second else 'B/')


class VideoScreenshot(object):
    def __init__(self, surface):
        self.surface = surface
        self.image = None

    @classmethod
    def filename_generator(cls):
        settings = BlinkSettings()
        name = os.path.join(settings.screenshots_directory.normalized, 'VideoCall-{:%Y%m%d-%H.%M.%S}'.format(datetime.now()))
        yield '%s.png' % name
        for x in count(1):
            yield "%s-%d.png" % (name, x)

    def capture(self):
        try:
            self.image = self.surface._image.copy()
        except AttributeError:
            pass
        else:
            player = WavePlayer(SIPApplication.alert_audio_bridge.mixer, Resources.get('sounds/screenshot.wav'), volume=30)
            SIPApplication.alert_audio_bridge.add(player)
            player.start()

    @run_in_thread('file-io')
    def save(self):
        if self.image is not None:
            filename = next(filename for filename in self.filename_generator() if not os.path.exists(filename))
            makedirs(os.path.dirname(filename))
            self.image.save(filename)


