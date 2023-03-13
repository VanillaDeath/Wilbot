from __future__ import annotations
import sys
import os
import datetime
import time
import re
import pytz
import mastodon # type: ignore
import megahal # type: ignore
import requests
import json
import bs4
import configparser
from prompt_toolkit import prompt
from prompt_toolkit import print_formatted_text as print
from prompt_toolkit import PromptSession
from prompt_toolkit import HTML
from prompt_toolkit.shortcuts import ProgressBar
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.patch_stdout import patch_stdout
from prompt_toolkit.shortcuts import yes_no_dialog

class Listener(mastodon.StreamListener):
    def __init__(self, bot_object):
        self.bot = bot_object
        super().__init__()

    def on_notification(self, notification):
        self.bot.handle_notification(notification)

    def handle_heartbeat(self):
        self.bot.auto_post()

    def on_abort(self, err):
        print(f"Error: {err.message}")
        self.bot.process_missed_events()

class Wilbot:
    visibilities = {'public': '🌎', 'unlisted': '🔓', 'private': '🔒', 'direct': '@'}
    command_prefix = '/'
    commands = {'help': ('help', '?', 'h', ''),
                'say': ('say', 'post', 'toot', 'publish'),
                'msg': ('msg', 'privmsg', 'd', 'dm', 'direct', 'pm', 'message'),
                'exit': ('exit', 'quit', 'q', 'close'),
                'learn': ('learn'),
                'train': ('train'),
                'block': ('block', 'ban'),
                'unblock': ('unblock', 'unban'),
                'blocks': ('blocks', 'bans'),
                'info': ('info', 'stats', 'information', 'statistics'),
                'tail': ('tail', 'log')}
    recap_log_lines = 20
    units = {'metric': 'C', 'imperial': 'F', 'kelvin': 'K'}
    weather_url = 'http://api.openweathermap.org/data/2.5/weather?'
    timeout = 300
    reconnect_wait = 5
    response_time = 0.1

    def __init__(self, config_file: str = 'wilbot.ini') -> None:
        # Set state
        self.init = False # True after successful __init__ complete
        self.run = False # True after successful __enter__ complete, set back to False to quit

        # Set up instance variables
        self.config = configparser.ConfigParser()
        if not os.path.exists(config_file):
            self.config['DEFAULT'] = {
                'instance_url': '',
                'access_token': 'token.secret',
                'time_zone': 'UTC',
                'max_post_length': '500',
                'auto_post': 'True',
                'auto_times': '12:00'
            }
            self.config['weather'] = {
                'api_key': '',
                'city_name': '',
                'units': 'metric'
            }
            with open(config_file, 'a') as conf_file_handle:
                self.config.write(conf_file_handle)
            print(HTML(
                "·Get your bot user's access token from Mastodon and put it in a file called <b>token.secret</b>\n"
                "·Make a field called <b>status</b> in your bot user's profile\n"
                "·Clear your bot user's notifications unless you want wilbot.py to parse and action them all on first run"))

        self.config.read(config_file)

        if len(self.config['DEFAULT']['instance_url']) == 0:
            print(f"Edit {config_file} with your settings before running again...")
            return

        (self.instance_url, self.access_token, self.max_post_length, self.time_zone,
         self.auto, self.auto_times, self.weather_api_key, self.weather_city_name, self.weather_units) = [
            self.config['DEFAULT']['instance_url'], self.config['DEFAULT']['access_token'],
            self.config.getint('DEFAULT', 'max_post_length'), self.config['DEFAULT']['time_zone'],
            self.config.getboolean('DEFAULT', 'auto_post'), Wilbot.csv_to_tuple(self.config['DEFAULT']['auto_times']),
            self.config['weather']['api_key'], self.config['weather']['city_name'], self.config['weather']['units']
         ]

        self.init = True

    def __enter__(self) -> Wilbot:
        # Short-circuit if __init__ didn't finish
        if not self.init:
            return self
        
        # Connect to Mastodon (stop if fails)
        try:
            self.mdon = mastodon.Mastodon(
                access_token=self.access_token,
                api_base_url=self.instance_url
                )
        except Exception as err:
            self.log_error(err, "initializing Mastodon connection", log_to_file=False)
            return self

        # Set up bot identification variables
        self.info = self.mdon.me()
        self.id = self.info['id']
        self.username = self.info['username']
        self.acct = self.info['acct']

        # Set files to names based on bot name
        self.brain_filename = f'{self.username}.brn'
        self.log_filename = f'{self.username}_{self.ts("%Y")}.log'
        self.last_filename = f'{self.username}.last'

        # Show lines of log file from last session(s)
        self.do_tail()

        # Start MegaHAL (stop if fails)
        self.log(f"Opening database {self.brain_filename}")
        try:
            self.mhal = megahal.MegaHAL(
                brainfile=self.brain_filename, timeout=Wilbot.response_time, max_length=self.max_post_length
                )
        except Exception as err:
            self.log_error(err, "initializing MegaHAL")
            return self

        # If auto-posting enabled, get last auto-post time from file
        if self.auto:
            with open(self.last_filename, 'a+') as last:
                last.seek(0)
                self.auto_last = int(last.readline().strip('\n') or 0)

        # Catch up on any missed notifications since last online
        self.process_missed_events()

        # Set up Mastodon stream listener (stop if fails)
        try:
            self.listener = Listener(self)
            self.handle = self.mdon.stream_user(listener=self.listener, run_async=True, timeout=Wilbot.timeout,
                                reconnect_async=True, reconnect_async_wait_sec=Wilbot.reconnect_wait)
            self.log(f"Listening ({self.instance_url})")
        except Exception as err:
            self.log_error(err, "starting Mastodon listener")
            return self

        # Set state
        self.run = True

        # Set bot to Online
        self.status(online=True)

        return self

    def __exit__(self, *a) -> None:
        if not self.init:
            return
        # Set bot to Offline
        if self.online:
            self.status(online=False)
        # Close MegaHAL database
        if self.mhal:
            self.mhal.close()
            del self.mhal
        # Add a blank line to log file to separate sessions
        if self.log_filename:
            self.log("", print_to_screen=False, prepend_timestamp=False)
        # Unset major instance variables
        del self.info, self.id, self.username, self.acct, self.handle, self.listener, self.mdon

    def ts(self, date_format: str = '%Y-%m-%d, %H:%M:%S', from_timestamp: int = -1) -> str:
        """Returns a date+time string"""
        if from_timestamp == -1:
            return str(datetime.datetime.now(pytz.timezone(self.time_zone)).strftime(date_format))
        return str(datetime.datetime.fromtimestamp(from_timestamp, pytz.timezone(self.time_zone)).strftime(date_format))

    def log(self, message: str, print_to_screen: bool = True, log_to_file: bool = True, prepend_timestamp: bool = True) -> bool:
        """Log to screen/file/both and optionally prepend with timestamp"""
        if prepend_timestamp:
            message = f"[{self.ts()}] {message}"
        if print_to_screen:
            print(message)
        if log_to_file:
            try:
                with open(self.log_filename, 'a') as logfile:
                    logfile.write(message+"\n")
            except Exception as err:
                print("Couldn't write to log file.")
                print(err)
                return False
        return True
    
    def log_error(self, err, message: str = "", log_to_file: bool = True) -> None:
        self.log(f"* @{self.acct} experienced an error{(' ' + message) if message else ''}", log_to_file=log_to_file)
        print(err)

    def process_missed_events(self) -> list:
        """Iterates through missed events and handles them"""
        h = []
        i = 0
        bottom_toolbar = HTML(" <b>[Esc]</b> Cancel")
        kb = KeyBindings()
        cancel = [False]

        @kb.add('escape')
        def _(event):
            cancel[0] = True

        with patch_stdout():
            with ProgressBar(title="Catching up on any missed events...", key_bindings=kb, bottom_toolbar=bottom_toolbar) as pb:
                notification: dict
                for notification in pb(self.mdon.notifications(), label='Notifications'):
                    if cancel[0]:
                        break
                    if handled := self.handle_notification(notification):
                        h.append(handled)
                    i += 1
                    #i += 1 if not cancel[0] and self.handle_notification(notification) else 0
                    if cancel[0]:
                        break
        print(HTML(("Cancelled" if cancel[0] else "Done") + f" (<b>{len(h)}</b> actionable / {i} total)"))
        return h

    def status(self, online: bool = True) -> dict | None:
        """Logs bot status, updates it on Mastodon profile"""
        self.online = online
        status = "🟢ONLINE" if self.online else "🔴OFFLINE"
        self.log(status)
        try:
            acct_update = self.mdon.account_update_credentials(fields=[('status', f"{status} since {self.ts()}")])
        except Exception as err:
            self.log_error(err, "updating status in profile")
            return None
        return acct_update

    def parse_notification(self, notification: dict) -> dict | bool | None:
        """Handles Mastodon events"""
        acct = notification['account']
        acct_id = acct['id']
        # Not sure if you can get notifications about yourself, but here's a circuit-breaker anyway
        if acct_id == self.id:
            return False
        acct_name = acct['acct']

        n_type = notification["type"]

        if n_type == 'follow':
            self.log(f"🤝 @{acct_name} ({str(acct_id)}) follows @{self.acct}")
            return True

        status = notification['status']
        content = status['content'].replace('<br>', '</p><p>')
        visibility = status['visibility']

        # Raw message, with HTML, mentions, and hashtags
        soup = bs4.BeautifulSoup(content, features='lxml')
        # Message without HTML
        text = ' '.join([s.get_text() for s in soup('p')])
        if n_type == 'status' and re.search("(?i)@"+self.username, text):
            # Mentioned!
            return False
        # Message without HTML, mentions, or hashtags
        message = Wilbot.strip_special(text)
        #if len(message) == 0:
            # Empty msg!
            #return False
        
        self.log(f"{Wilbot.visibilities[visibility]} <@{acct_name}> {text}")
        learn = visibility == 'public' and len(message) > 0
        learn_msg = f"💭 @{self.acct} learns from @{acct_name}: {message}" if learn else f"🚫 Not learning post ({'visibility not public' if visibility != 'public' else 'empty message'})"

        if n_type == 'status':
            self.log(learn_msg)
            if visibility != 'public':
                return False
            try:
                self.mhal.learn(message)
                self.mhal.sync()
            except Exception as err:
                self.log_error(err, "learning new string")
                return False
            return True

        if n_type == 'mention':
            reply_visibility = visibility if visibility != 'public' else 'unlisted'
            match message.lower():
                case 'follow':
                    return self.do_follow_unfollow(acct, follow=True)
                case 'unfollow':
                    return self.do_follow_unfollow(acct, follow=False)
                case 'help' | '?':
                    reply_message = self.help_user()
                case _:
                    max_length = self.max_post_length - len(f"@{acct_name} ")
                    try:
                        if learn:
                            reply_message = Wilbot.format_reply(self.mhal.get_reply(message, max_length=max_length), max_length)
                            self.mhal.sync()
                        else:
                            reply_message = Wilbot.format_reply(self.mhal.get_reply_nolearn(message, max_length=max_length), max_length)
                        self.log(learn_msg)
                    except Exception as err:
                        self.log_error(err, "retrieving a reply")
                        reply_message = "ERROR!"
            try:
                status = self.mdon.status_reply(
                    to_status=status, status=reply_message, visibility=reply_visibility, untag=True)
                self.log(
                    f"{Wilbot.visibilities[reply_visibility]} <@{self.acct}> @{acct_name} " + reply_message.replace('\n', ' '))
            except Exception as err:
                self.log_error(err, "posting to Mastodon")
                return False
            return status
        return None

    def handle_notification(self, notification: dict) -> bool:
        """Filter for handling Mastodon events, clears notification either way"""
        handle = notification['type'] in ['mention', 'follow', 'status']
        handled = bool(self.parse_notification(notification)) if handle else False
        self.mdon.notifications_dismiss(notification['id'])
        return handled

    def post(self, message, visibility: str = 'public', is_auto: bool = False) -> dict | None:
        """Posts a status to Mastodon"""
        try:
            status = self.mdon.status_post(status=message, visibility=visibility)
            self.log(f"{Wilbot.visibilities[visibility]} {'⏰' if is_auto else ''} <@{self.acct}> {message}")
        except Exception as err:
            self.log_error(err, "attempting to post status")
            return None
        return status

    def auto_post(self, post_time: bool = True, post_weather: bool = True) -> dict | None:
        """Checks to see if it's time to auto-post, if it is then does so"""
        
        # Short-circuit out if auto-posting disabled, it is not time to auto-post,
        # or already auto-posted in last minute
        if (not self.auto
            or (timestr := self.ts(date_format='%H:%M')) not in self.auto_times
            or ((now := int(time.time())) - self.auto_last) < 60):
            return None
        
        # Time and weather (optional) strings
        it_is_time = f"It is {timestr}. " if post_time else ""
        weather = ""
        if post_weather and self.weather_api_key:
            wx = requests.get(f'{Wilbot.weather_url}appid={self.weather_api_key}&units={self.weather_units}&q={self.weather_city_name}').json()
            if wx['cod'] != '404':
                weather = f" The weather in {wx['name']} is {str(round(wx['main']['temp']))}°{Wilbot.units[self.weather_units]} and {wx['weather'][0]['description']}."
        
        # Get MegaHAL reply
        this_max_length = self.max_post_length - len(it_is_time) - len(weather)
        try:
            this_post = it_is_time + Wilbot.format_reply(self.mhal.get_reply_nolearn("", max_length=this_max_length), this_max_length) + weather
        except Exception as err:
            self.log_error(err, "retrieving a reply")
            return None
        # Post to Mastodon
        try:
            status = self.post(this_post, visibility='unlisted', is_auto=True)
        except Exception as err:
            self.log_error(err, "posting to Mastodon")
            return None
        
        # Update last auto-post time
        self.auto_last = now
        with open(self.last_filename, 'w') as last:
            last.write(str(self.auto_last))
        return status

    def do(self, input_string: str):
        """Handles command inputs. Returns False for exit, True otherwise."""
        input_string = input_string.strip()
        prefix_length = len(Wilbot.command_prefix)
        # No command, just give a reply (nolearn) to the input
        if input_string[0:prefix_length] != Wilbot.command_prefix:
            try:
                print(f"<@{self.acct}> {self.mhal.get_reply_nolearn(input_string)}")
                return True
            except Exception as err:
                self.log_error(err, "retrieving a reply")
                return False
        # Command entered, act accordingly
        (command, _, params) = input_string[prefix_length:].partition(' ')
        match Wilbot.is_in(command):
            case 'help':
                return self.do_help()
            case 'exit':
                return self.do_exit()
            case 'say':
                return self.do_say_msg(params, is_private=False)
            case 'msg':
                return self.do_say_msg(params, is_private=True)
            case 'learn':
                return self.do_learn(params)
            case 'train':
                return self.do_train(params)
            case 'block':
                return self.do_block_unblock(params, block=True)
            case 'unblock':
                return self.do_block_unblock(params, block=False)
            case 'blocks':
                return self.do_blocks()
            case 'info':
                return self.do_info()
            case 'tail':
                return self.do_tail()
            case _:
                print("Unknown command")
                return False

    def do_exit(self) -> bool:
        self.run = not yes_no_dialog(title="Exit", text="Stop the bot and exit?").run()
        return self.run

    def do_follow_unfollow(self, acct: dict, follow=True) -> dict | None:
        try:
            new_relationship = self.mdon.account_follow(acct['id'], reblogs=False, notify=True) if follow else self.mdon.account_unfollow(acct['id'])
            self.log(
                f"{'✔️' if follow else '❌'} @{self.acct} {'' if follow else 'un'}follows @{acct['acct']} ({str(acct['id'])})")
        except Exception as err:
            self.log_error(
                err, f"attempting to {'' if follow else 'un'}follow @{acct['acct']}")
            return None
        return new_relationship

    def do_say_msg(self, message: str, is_private: bool = False) -> dict | None:
        """Manually make the bot say something"""
        message = Wilbot.get_message(message, "Message? [leave blank for random]")
        if len(message) == 0:
            try:
                message = self.mhal.get_reply_nolearn("", max_length=self.max_post_length)
            except Exception as err:
                self.log_error(err, "retrieving a reply")
                return None
            print(f'"{message}"')
        visibility = 'direct' if is_private else prompt(f"Visibility? [{'/'.join(x for x in Wilbot.visibilities)}/CANCEL] ").lower()
        if visibility not in Wilbot.visibilities or not Wilbot.confirm(f'{Wilbot.visibilities[visibility]} Say "{message}"?'):
            return Wilbot.cancelled()
        return self.post(message, visibility)

    def do_train(self, filename: str) -> bool:
        """Make the bot learn from a file of strings"""
        filename = Wilbot.get_message(filename, "Filename:")
        if len(filename) == 0 or not Wilbot.confirm(f"Learn from {filename}?"):
            return Wilbot.cancelled_False()
        try:
            self.mhal.train(filename)
            self.mhal.sync()
        except Exception as err:
            self.log_error(err, "training MegaHAL")
            return False
        self.log(f"💭 @{self.username} trains on {filename}")
        return True

    def do_learn(self, message: str) -> bool:
        """Make the bot learn a string"""
        message = Wilbot.get_message(message, "String to learn:")
        if len(message) == 0 or not Wilbot.confirm(f'Learn "{message}"?'):
            return Wilbot.cancelled_False()
        try:
            self.mhal.learn(message)
            self.mhal.sync()
        except Exception as err:
            self.log_error(err, "learning string")
            return False
        self.log(f"💭 @{self.username} learns from manual input: {message}")
        return True

    def do_block_unblock(self, target: str, block: bool = True) -> bool | dict:
        """Block a user/domain if block==True, else unblock the user/domain"""
        target = Wilbot.get_message(target, f"User/domain to {'' if block else 'un'}block:")
        if len(target) == 0:
            return Wilbot.cancelled_False()
        
        # Domain block
        # If not username or username@domain.tld
        if not re.match(r"^([a-zA-Z0-9_%+-]+)(@([a-zA-Z0-9.-]+\.[a-zA-Z]{2,}))?$", target):
            # If not domain.tld then abort
            if not re.match(r"^([a-zA-Z0-9.-]+\.[a-zA-Z]{2,})$", target):
                print("⚠️ Make sure you're using username@domain.tld or domain.tld format")
                return False
            if not Wilbot.confirm(f"{'Block' if block else 'Unblock'} {target}?"):
                return Wilbot.cancelled_False()
            self.mdon.domain_block(target) if block else self.mdon.domain_unblock(target)
            self.log(f"{'⛔' if block else '🆗'} @{self.username} {'' if block else 'un'}blocks domain {target}")
            return True

        # User account block
        try:
            account = self.mdon.account_lookup(target)
        except mastodon.MastodonNotFoundError:
            print(f"⚠️ Account {target} not found")
            return False
        acct_with_id = f"{account['acct']} ({account['id']})"
        blocked = 0
        # Check relationship status
        for relationship in self.mdon.account_relationships(account['id']):
            # If block requested
            if block:
                # If following, ask whether to unfollow
                if relationship['following'] and Wilbot.confirm(f"Unfollow {acct_with_id}?"):
                        self.do_follow_unfollow(account, follow=False)
                # If already blocking, cancel
                if relationship['blocking']:
                    print(f"⚠️ Already blocking {acct_with_id}")
                    return False
                # If already blocking the domain for the account, notify but don't cancel
                if relationship['domain_blocking']:
                    print(f"Already blocking the domain for {acct_with_id}")
            # If unblock requested
            else:
                # Their account is blocked, let's proceed
                if relationship['blocking']:
                    blocked = 1
                    break
                # Their domain is blocked
                if relationship['domain_blocking']:
                    blocked = -1
        # If unblock requested but account itself is not blocked, notify (and specify if entire domain is blocked) and cancel
        if not block and blocked != 1:
            print(f"⚠️ User {acct_with_id} is not blocked{', their entire domain is' if blocked == -1 else ''}")
            return False
        if not Wilbot.confirm(f"{'Block' if block else 'Unblock'} {acct_with_id}?"):
            return Wilbot.cancelled_False()
        try:
            new_relationship = self.mdon.account_block(account['id']) if block else self.mdon.account_unblock(account['id'])
            self.log(f"{'⛔' if block else '🆗'} @{self.username} {'' if block else 'un'}blocks {acct_with_id}")
        except Exception as err:
            self.log_error(
                err, f"attempting to {'' if block else 'un'}block {acct_with_id}")
            return False
        return new_relationship

    def do_blocks(self) -> None:
        """List blocks"""
        print("⛔ Blocked users:")
        for block in self.mdon.blocks():
            print(f"· {block['acct']} ({block['id']})")
        print("⛔ Blocked domains:")
        for block in self.mdon.domain_blocks():
            print(f"· {block}")

    def do_info(self) -> None:
        self.info = self.mdon.me()
        print(HTML(f"<b>{self.acct}</b> ({self.info['display_name']}) has <b>{self.info['followers_count']}</b> followers, is following <b>{self.info['following_count']}</b> users, "
                   f"posted <b>{self.info['statuses_count']}</b> statuses, and blocks <b>{len(self.mdon.blocks())}</b> users and <b>{len(self.mdon.domain_blocks())}</b> domains"))

    def do_tail(self) -> None:
        Wilbot.tail(filename=self.log_filename, lines=Wilbot.recap_log_lines, wrap=True)

    def do_help(self) -> None:
        """Print botadmin help"""
        c = Wilbot.command_prefix
        print(HTML(f"·<b>{c}help</b>: This help message             ·<b>{c}exit</b>: Close bot\n"
                   f"·<b>{c}say</b>: Post a toot                    ·<b>{c}msg</b>: Direct message somebody\n"
                   f"·<b>{c}learn</b>: Manually learn a new string  ·<b>{c}train</b>: Manually learn a file of strings\n"
                   f"·<b>{c}block</b>: Block a user or domain       ·<b>{c}unblock</b>: Unblock a user or domain\n·<b>{c}blocks</b>: List blocked users and domains\n"
                   f"·<b>{c}info</b>: Bot information               ·<b>{c}tail</b>: Last {Wilbot.recap_log_lines} lines of log file\n"
                   f" Legend: {' | '.join('<ansiblue>' + v + ' ' + k + '</ansiblue>' for k,v in Wilbot.visibilities.items())} | <ansiyellow>🤝 user follows {self.username}</ansiyellow> | <ansibrightgreen>✔️  {self.username} follows user</ansibrightgreen> | <ansired>❌  {self.username} unfollows user </ansired>| <ansimagenta>💭{self.username} learns</ansimagenta>"))

    def help_user(self) -> str:
        """Return user help"""
        return f"Hi! My name is {self.username}. I am based on the Wilbot python project: A MegaHAL chatbot for Mastodon in beta.\n\nI like nonsense. I read and learn public posts from the users I follow so I can turn them into word salad later!\n\nIf you'd like me to follow you, send me:\n@{self.acct} follow\n\nLikewise, to get me to stop, send me:\n@{self.acct} unfollow\n\nIf you mention me, I will reply to you... Except if I have crashed, then I will do nothing, sorry."

    @staticmethod
    def read_n_to_last_line(filename: str, n: int = 1) -> str:
        """Returns the nth before last line of a file (n=1 gives last line)"""
        num_newlines = 0
        with open(filename, 'rb') as f:
            try:
                f.seek(-2, os.SEEK_END)
                while num_newlines < n:
                    f.seek(-2, os.SEEK_CUR)
                    if f.read(1) == b'\n':
                        num_newlines += 1
            except OSError:
                f.seek(0)
            last_line = f.readline().decode()
        return last_line

    @staticmethod
    def tail(filename: str, lines: int = 20, wrap: bool = True) -> None:
        """Grabs and displays last x log lines"""
        try:
            if wrap:
                print(f"--- Last {lines} log lines:")
            for x in range(lines, 0, -1):
                print(Wilbot.read_n_to_last_line(filename, n=x), end="")
            if wrap:
                print("---")
        except:
            print("Cannot read log file.")

    @staticmethod
    def strip_special(message: str) -> str:
        """Removes mentions, converts hashtags to words, removes excess spaces"""
        # Remove mentions
        message = re.sub(r"@([a-zA-Z0-9_%+-]+)(@([a-zA-Z0-9.-]+\.[a-zA-Z]{2,}))?", r"", message)
        # Remove hash from hashtags
        message = re.sub(r"#([^\s]+)", r"\1", message)
        # Convert multiple whitespace to single space
        message = re.sub(r"\s\s+", r" ", message)
        # Strip leading/trailing space
        return message.strip()

    @staticmethod
    def format_reply(message: str, max_length: int) -> str:
        """Converts a megahal Reply object to string, formats, and truncates it for posting"""
        return Wilbot.strip_special(str(message))[:max_length]

    @staticmethod
    def get_message(message: str, prmpt: str = "Message?") -> str:
        """Prompts for message if nothing was inputted after commands that accept parameters"""
        message = message.strip()
        if len(message) == 0:
            return prompt(f"{prmpt} ").strip()
        return message

    @staticmethod
    def confirm(prmpt: str = "Are you sure?", default: str = 'n') -> bool:
        """Shows a yes/no prompt and returns True for yes, False for no"""
        yn = "Y/n" if default == 'y' else "y/N"
        c = prompt(f"{prmpt} [{yn}] ")
        return c.lower() == 'y' or c.lower() == 'yes' or (default == 'y' and c == '')

    @staticmethod
    def is_in(inp: str) -> str | None:
        """If inp is in one of the command tuples listed in Wilbot.commands, return the command, else None"""
        for command,acceptable in Wilbot.commands.items():
            if inp.lower() in acceptable:
                return command
        return None
    
    @staticmethod
    def cancelled() -> None:
        print("Cancelled")

    @staticmethod
    def cancelled_False() -> bool:
        Wilbot.cancelled()
        return False

    @staticmethod
    def csv_to_tuple(s: str) -> tuple:
        return tuple(i.strip() for i in s.split(','))


def main(args: list) -> int:
    """Interactive bot back-end"""

    config_file = args[1] if len(args) > 1 else 'wilbot.ini'

    with Wilbot(config_file=config_file) as wilbot:

        if not wilbot.run:
            return 1

        wilbot.do_help()
        wilbot.do_info()

        session: PromptSession = PromptSession()

        # Main loop
        while wilbot.run:
            wilbot.do(session.prompt(HTML("<ansibrightgreen>></ansibrightgreen> ")))

    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv))