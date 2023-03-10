# Wilbot
Wilbot is a MegaHAL chatbot for Mastodon written in python. 

Install the following modules:
```bash
pip3 install Mastodon.py megahal bs4 pytz
```
 
Run:
```bash
python3 wilbot.py
```

Copy your bot user's access token to a file called token.secret in the same directory as wilbot.py. 

Config (after first run) in wilbot.ini. Set instance_url to the URL of the Mastodon instance (e.g. https://example.com) and timezone to your timezone string (e.g. America/St_Johns), and that's it. 

You can also configure whether and when the bot auto-posts, and you can add an openweathermap.org API key and city name to have it announce the current weather at the end of these auto-posts. The auto_times string is a series of comma-separated 24-hour H:MM times, e.g.:
```ini
auto_times = 0:00, 6:00, 12:00, 18:00
```
 
More options are available in the interactive back-end that runs when you start the script. The bot is online until you use /exit. It is recommended to run it in a tmux or screen session instead of forking into the background so that you have access to this back-end.