"""Graphical notifications for the hangups UI.

TODO:
    - Support other notification systems (like terminal bell).
    - Support notify-osd's merged notifications. It appears this would require
    using a dbus library so that each notification comes from the same process.
    - Create notifications for other events like (dis)connection
"""

import html
import logging
import re
import subprocess

logger = logging.getLogger(__name__)
NOTIFY_CMD = [
    'gdbus', 'call', '--session', '--dest', 'org.freedesktop.Notifications',
    '--object-path', '/org/freedesktop/Notifications', '--method',
    'org.freedesktop.Notifications.Notify', 'hangups', '{replaces_id}', '',
    '{sender_name}', '{msg_text}', '[]', '{{}}', ' -1'
]
RESULT_RE = re.compile(r'\(uint32 ([\d]+),\)')


class Notifier(object):

    """Receives events from hangups and creates system notifications.

    This uses the gdbus utility to create freedesktop.org notifications. If a
    new notification is created while a previous one is still open, the
    previous notification is instantly replaced.
    """

    def __init__(self, client, conv_list):
        self._conv_list = conv_list  # hangups.ConversationList
        self._client = client  # hangups.Client
        self._client.on_message += self._on_message
        self._replaces_id = 0

    def _on_message(self, client, conv_id, user_id, timestamp, text):
        """Create notification for new messages."""
        conv = self._conv_list.get(conv_id)
        user = conv.get_user(user_id)
        # Ignore messages sent by yourself.
        if not user.is_self:
            # We have to escape angle brackets because freedesktop.org
            # notifications support markup.
            cmd = [arg.format(
                sender_name=html.escape(user.full_name, quote=False),
                msg_text=html.escape(text, quote=False),
                replaces_id=self._replaces_id,
            ) for arg in NOTIFY_CMD]

            # Run the notification and parse out the replaces_id. Since the
            # command is a list of arguments, and we're not using a shell, this
            # should be safe.
            logger.info('Creating notification with command: {}'.format(cmd))
            try:
                output = subprocess.check_output(
                    cmd, stderr=subprocess.STDOUT
                ).decode()
            except (subprocess.CalledProcessError, FileNotFoundError) as e:
                logger.warning('Notification command failed: {}'.format(e))
                return
            try:
                self._replaces_id = RESULT_RE.match(output).groups()[0]
            except (AttributeError, IndexError) as e:
                logger.warning('Failed to parse notification command '
                               'result: {}'.format(e))
