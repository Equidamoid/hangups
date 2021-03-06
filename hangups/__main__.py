"""Demo chat client using Hangups."""

# "unused argument" are unavoidable because of obsub events.
# pylint: disable=W0613

from tornado import ioloop
import appdirs
import argparse
import datetime
import logging
import os
import sys
import urwid

import hangups
from hangups.notify import Notifier


LOG_FORMAT = '%(asctime)s - %(name)s - %(levelname)s - %(message)s'
COL_SCHEMES = {
    # Very basic scheme with no colour
    'default': {
        ('active_tab', '', ''),
        ('inactive_tab', 'standout', ''),
        ('msg_date', '', ''),
        ('msg_sender', '', ''),
        ('msg_text', '', ''),
        ('status_line', 'standout', ''),
        ('tab_background', 'standout', ''),
    },
    'solarized-dark': {
        ('active_tab', 'light gray', 'light blue'),
        ('inactive_tab', 'underline', 'light green'),
        ('msg_date', 'dark cyan', ''),
        ('msg_sender', 'dark blue', ''),
        ('msg_text', '', ''),
        ('status_line', 'standout', ''),
        ('tab_background', 'underline', 'black'),
    },
}


class ChatUI(object):
    """User interface for hangups."""

    def __init__(self, cookies_path, keybindings, palette):
        """Start the user interface."""
        self._keys = keybindings

        # These are populated by on_connect when it's called.
        self._conv_widgets = {} # {conversation_id: ConversationWidget}
        self._tabbed_window = None # TabbedWindowWidget
        self._conv_list = None # hangups.ConversationList
        self._user_list = None # hangups.UserList
        self._notifier = None # hangups.notify.Notifier

        # TODO Add urwid widget for getting auth.
        try:
            cookies = hangups.auth.get_auth_stdin(cookies_path)
        except hangups.GoogleAuthError as e:
            print('Login failed ({})'.format(e))
            sys.exit(1)

        self._client = hangups.Client(cookies)
        self._client.on_connect += self._on_connect
        self._client.on_disconnect += self._on_disconnect
        self._client.on_message += self._on_message

        class MyEventLoop(urwid.TornadoEventLoop):
            """Patched Tornado event loop for urwid.

            Patch urwid's TornadoEventLoop to run_sync() our connection
            coroutine rather than calling start(). This "fixes" exception
            handling, causing all uncaught exceptions to be fatal.
            """
            _client = self._client
            def run(self):
                try:
                    ioloop.IOLoop.instance().run_sync(self._client.connect)
                except ioloop.TimeoutError:
                    # Ignore spurious timeout when there's another exception.
                    pass
                # Raise exception if there was one.
                if self._exception:
                    exc, self._exception = self._exception, None
                    raise exc # pylint: disable=E0702

        # Initialize urwid, starting the IOLoop, and block until the IOLoop
        # exits.
        self._urwid_loop = urwid.MainLoop(LoadingWidget(), palette,
                                          event_loop=MyEventLoop(),
                                          handle_mouse=False)
        self._urwid_loop.run()

    def get_conv_widget(self, conv_id):
        """Return an existing or new ConversationWidget."""
        if conv_id not in self._conv_widgets:
            set_title_cb = (lambda widget, title:
                            self._tabbed_window.set_tab(widget, title=title))
            widget = ConversationWidget(self._client,
                                        self._conv_list.get(conv_id),
                                        set_title_cb)
            self._conv_widgets[conv_id] = widget
        return self._conv_widgets[conv_id]

    def add_conversation_tab(self, conv_id, switch=False):
        """Add conversation tab if not present, and optionally switch to it."""
        conv_widget = self.get_conv_widget(conv_id)
        self._tabbed_window.set_tab(conv_widget, switch=switch)

    def on_select_conversation(self, conv_id):
        """Called when the user selects a new conversation to listen to."""
        # switch to new or existing tab for the conversation
        self.add_conversation_tab(conv_id, switch=True)

    def _on_connect(self, client):
        """Handle connecting for the first time."""
        self._conv_list = hangups.ConversationList(self._client)
        self._user_list = hangups.UserList(self._client)
        self._notifier = Notifier(self._client, self._conv_list)
        # show the conversation menu
        conv_picker = ConversationPickerWidget(self._conv_list,
                                               self.on_select_conversation)
        self._tabbed_window = TabbedWindowWidget(self._keys)
        self._tabbed_window.set_tab(conv_picker, switch=True,
                                    title='Conversations')
        self._urwid_loop.widget = self._tabbed_window

    def _on_message(self, client, conv_id, user_id, timestamp, text):
        """Open conversation tab for new messages when they arrive."""
        self.add_conversation_tab(conv_id)

    def _on_disconnect(self, client):
        """Handle disconnecting."""
        # TODO: handle this
        print('Connection lost')


def get_conv_name(conv, truncate=False):
    """Return the readable name for a conversation.

    For one-to-one conversations, the name is the full name of the other user.
    For group conversations, the name is a comma-separated list of first names.

    If truncate is true, only show up to two names in a group conversation.
    """
    participants = sorted(user for user in conv.users if not user.is_self)
    names = [user.first_name for user in participants]
    if len(participants) == 1:
        return participants[0].full_name
    elif truncate and len(participants) > 2:
        return ', '.join(names[:2] + ['+{}'.format(len(names) - 2)])
    else:
        return ', '.join(names)


class LoadingWidget(urwid.WidgetWrap):
    """Widget that shows a loading indicator."""

    def __init__(self):
        # show message in the center of the screen
        super().__init__(urwid.Filler(
            urwid.Text('Connecting...', align='center')
        ))


class ConversationPickerWidget(urwid.WidgetWrap):
    """Widget for picking a conversation."""

    def __init__(self, conversation_list, on_select):
        # Build buttons for selecting conversations ordered by most recently
        # modified first.
        convs = sorted(conversation_list.get_all(), reverse=True,
                       key=lambda c: c.last_modified)
        on_press = lambda button, conv_id: on_select(conv_id)
        buttons = [urwid.Button(get_conv_name(conv), on_press=on_press,
                                user_data=conv.id_)
                   for conv in convs]
        listbox = urwid.ListBox(urwid.SimpleFocusListWalker(buttons))
        widget = urwid.Padding(listbox, left=2, right=2)
        super().__init__(widget)


class ReturnableEdit(urwid.Edit):
    """Edit widget that clears itself and calls a function on return."""

    def __init__(self, on_return, caption=None):
        super().__init__(caption=caption)
        self._on_return = on_return

    def keypress(self, size, key):
        key = super().keypress(size, key)
        if key == 'enter':
            self._on_return(self.get_edit_text())
            self.set_edit_text('')
        else:
            return key


class StatusLineWidget(urwid.WidgetWrap):
    """Widget for showing typing status."""

    def __init__(self, conversation):
        self._typing_statuses = {}
        self._conversation = conversation
        self._conversation.on_message += self._on_message
        self._conversation.on_typing += self._on_typing
        self._widget = urwid.Text('', align='center')
        super().__init__(urwid.AttrWrap(self._widget, 'status_line'))

    def _on_message(self, conversation, user_id, timestamp, text):
        """Make users stop typing when they send a message."""
        self._typing_statuses[user_id] = 'stopped'
        self._update()

    def _on_typing(self, conversation, user_id, timestamp, status):
        """Handle typing updates."""
        self._typing_statuses[user_id] = status
        self._update()

    def _update(self):
        """Update list of typers."""
        typers = [self._conversation.get_user(user_id).first_name
                  for user_id, status in self._typing_statuses.items()
                  if status == 'typing']
        if len(typers) > 0:
            msg = '{} {} typing...'.format(
                ', '.join(sorted(typers)),
                'is' if len(typers) == 1 else 'are'
            )
        else:
            msg = ''
        self._widget.set_text(msg)


class ConversationWidget(urwid.WidgetWrap):
    """Widget for interacting with a conversation."""

    def __init__(self, client, conversation, set_title_cb):
        client.on_disconnect += lambda client: self._show_info_message(
            'Disconnected. Messages will not be received.'
        )
        client.on_reconnect += lambda client: self._show_info_message(
            'Connected.'
        )
        self._conversation = conversation
        self._conversation.on_message += self._on_message

        self._num_unread = 0
        self._set_title_cb = set_title_cb
        self._set_title()

        self._list_walker = urwid.SimpleFocusListWalker([])
        self._list_box = urwid.ListBox(self._list_walker)
        self._status_widget = StatusLineWidget(conversation)
        self._widget = urwid.Pile([
            ('weight', 1, self._list_box),
            ('pack', self._status_widget),
            ('pack', ReturnableEdit(self._on_return, caption='Send message: ')),
        ])
        # focus the edit widget by default
        self._widget.focus_position = 2

        # Display any old messages already attached to the conversation.
        for message in self._conversation.messages:
            self._on_message(*message)
        self._num_unread = 0
        self._set_title()

        super().__init__(self._widget)

    def keypress(self, size, key):
        """Handle keypresses marking messages as read."""
        self._num_unread = 0
        self._set_title()
        return super().keypress(size, key)

    def _set_title(self):
        """Update this conversation's tab title."""
        title = get_conv_name(self._conversation, truncate=True)
        if self._num_unread > 0:
            title += ' ({})'.format(self._num_unread)
        self._set_title_cb(self, title)

    def _on_return(self, text):
        """Called when the user presses return on the send message widget."""
        # Ignore if the user hasn't typed a message.
        if len(text) == 0:
            return
        # XXX: Exception handling here is still a bit broken. Uncaught
        # exceptions in _on_message_sent will only be logged.
        self._conversation.send_message(text).add_done_callback(
            self._on_message_sent
        )

    def _append_text(self, text):
        """Append text as a new line in the ConversationWidget.

        Automatically scroll down to show the new text.
        """
        self._list_walker.append(urwid.Text(text))
        # XXX: ListBox may not scroll enough to completely show the first
        # widget that goes off-screen. This can be fixed by repeating the
        # set_focus call, but this workaround causes other issues.
        self._list_box.set_focus(len(self._list_walker) - 1,
                                 coming_from='above')

    def _on_message_sent(self, future):
        """Handle showing an error if a message fails to send."""
        try:
            future.result()
        except hangups.NetworkError:
            self._show_info_message('Failed to send message.')

    @staticmethod
    def _get_date_str(timestamp=None):
        """Convert UTC datetime into user interface string."""
        if timestamp is None:
            timestamp = datetime.datetime.now(tz=datetime.timezone.utc)
        return timestamp.astimezone(tz=None).strftime('%I:%M:%S %p')

    def _show_info_message(self, text):
        """Display an informational message with timestamp."""
        self._append_text([
            ('msg_date', '(' + self._get_date_str() + ') '),
            ('msg_text', text),
        ])

    def _on_message(self, conversation, user_id, timestamp, text):
        """Display a new conversation message."""
        user = self._conversation.get_user(user_id)

        # Format the message and add it to the list box.
        self._append_text([
            ('msg_date', '(' + self._get_date_str(timestamp) + ') '),
            ('msg_sender', user.first_name + ': '),
            ('msg_text', text)
        ])

        # Update the count of unread messages.
        if not user.is_self:
            self._num_unread += 1
            self._set_title()


class TabbedWindowWidget(urwid.WidgetWrap):

    """A widget that displays a list of widgets via a tab bar."""

    def __init__(self, keybindings):
        self._widgets = [] # [urwid.Widget]
        self._widget_title = {} # {urwid.Widget: str}
        self._tab_index = None # int
        self._keys = keybindings
        self._tabs = urwid.Text('')
        self._frame = urwid.Frame(None)
        super().__init__(urwid.Pile([
            ('pack', urwid.AttrWrap(self._tabs, 'tab_background')),
            ('weight', 1, self._frame),
        ]))

    def _update_tabs(self):
        """Update tab display."""
        text = []
        for num, widget in enumerate(self._widgets):
            palette = ('active_tab' if num == self._tab_index
                       else 'inactive_tab')
            text += [
                (palette, ' {} '.format(self._widget_title[widget]).encode()),
                ('tab_background', b' '),
            ]
        self._tabs.set_text(text)
        self._frame.contents['body'] = (self._widgets[self._tab_index], None)

    def keypress(self, size, key):
        """Handle keypresses for changing tabs."""
        key = super().keypress(size, key)
        num_tabs = len(self._widgets)
        if key == self._keys['prev_tab']:
            self._tab_index = (self._tab_index - 1) % num_tabs
            self._update_tabs()
        elif key == self._keys['next_tab']:
            self._tab_index = (self._tab_index + 1) % num_tabs
            self._update_tabs()
        else:
            return key

    def set_tab(self, widget, switch=False, title=None):
        """Add or modify a tab.

        If widget is not a tab, it will be added. If switch is True, switch to
        this tab. If title is given, set the tab's title.
        """
        if widget not in self._widgets:
            self._widgets.append(widget)
            self._widget_title[widget] = ''
        if switch:
            self._tab_index = self._widgets.index(widget)
        if title:
            self._widget_title[widget] = title
        self._update_tabs()


def main():
    """Main entry point."""
    # Build default paths for files.
    dirs = appdirs.AppDirs('hangups', 'hangups')
    default_log_path = os.path.join(dirs.user_log_dir, 'hangups.log')
    default_cookies_path = os.path.join(dirs.user_cache_dir, 'cookies.json')

    parser = argparse.ArgumentParser(
        prog='hangups', formatter_class=argparse.ArgumentDefaultsHelpFormatter
    )
    dirs = appdirs.AppDirs('hangups', 'hangups')
    parser.add_argument('-d', '--debug', action='store_true',
                        help='log detailed debugging messages')
    parser.add_argument('--log', default=default_log_path,
                        help='log file path')
    parser.add_argument('--cookies', default=default_cookies_path,
                        help='cookie storage path')
    parser.add_argument('--key-next-tab', default='ctrl d',
                        help='keybinding for next tab')
    parser.add_argument('--key-prev-tab', default='ctrl u',
                        help='keybinding for previous tab')
    parser.add_argument('--col-scheme', choices=COL_SCHEMES.keys(),
                        default='default', help='colour scheme to use')
    args = parser.parse_args()

    # Create all necessary directories.
    for path in [args.log, args.cookies]:
        directory = os.path.dirname(path)
        if directory != '' and not os.path.isdir(directory):
            try:
                os.makedirs(directory)
            except OSError as e:
                sys.exit('Failed to create directory: {}'.format(e))

    log_level = logging.DEBUG if args.debug else logging.WARNING
    logging.basicConfig(filename=args.log, level=log_level, format=LOG_FORMAT)

    try:
        ChatUI(args.cookies, {
            'next_tab': args.key_next_tab,
            'prev_tab': args.key_prev_tab,
        }, COL_SCHEMES[args.col_scheme])
    except KeyboardInterrupt:
        pass
    except:
        # urwid will prevent some exceptions from being printed unless we use
        # print a newline first.
        print('')
        raise


if __name__ == '__main__':
    main()
