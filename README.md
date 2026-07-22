# Pi Social AutoPost

Turn a Raspberry Pi into a little robot that posts your videos to TikTok and
Instagram every day — even while you sleep.

You drop videos into a folder. On a schedule you choose, the Pi picks one at
random, posts it to both platforms at once, and files it away so it never
double-posts. When it runs out of fresh videos, it starts replaying old ones so
your accounts never go quiet. There's also a slick web page you can open on
your phone to upload videos, edit captions, and watch the queue.

I built this to promote my music, but it works for any short-form video
content: clips, memes, art timelapses, whatever you make.

## What you need

- **A Raspberry Pi** running Raspberry Pi OS. Even an old Pi 3 is plenty —
  this doesn't do any heavy lifting. You should be able to SSH into it (or
  have a keyboard and screen hooked up).
- **A free-ish [Zernio](https://zernio.com) account** with your TikTok and
  Instagram connected. Zernio is a service that handles the actual posting,
  so you don't have to beg TikTok and Meta for developer API access (trust
  me, you don't want to).
  - Heads up: Instagram needs to be a **Creator or Business** account to
    allow API posting. Switching is free in the Instagram app settings.
- **Python 3** — already on Raspberry Pi OS.
- About 20 minutes.

## Step 1 — Get the code onto your Pi

SSH into your Pi and run:

```bash
git clone https://github.com/YOURNAME/pi-social-autopost.git ~/pi-social-autopost
cd ~/pi-social-autopost
pip install -r requirements.txt --break-system-packages
```

(That `--break-system-packages` flag looks scary but it's just how newer
Raspberry Pi OS wants you to install Python packages. It's fine.)

## Step 2 — Get your Zernio API key and account IDs

1. In your Zernio dashboard, create an **API key**. It starts with `sk_`.
   Treat it like a password — anyone who has it can post as you.
2. Find your **account IDs**. Run this on the Pi (paste your real key in):

```bash
curl -s "https://zernio.com/api/v1/accounts" \
  -H "Authorization: Bearer sk_your_key_here"
```

You'll get a wall of text back. Look for `"_id"` near `"platform":"tiktok"`
and again near `"platform":"instagram"`. Those two ID strings are what you
need. (They're not secret, unlike the key.)

## Step 3 — Create your config file

This is a tiny text file that holds your key and IDs so they're not written
into the code itself:

```bash
mkdir -p ~/.config
cp ~/pi-social-autopost/autopost.env.example ~/.config/autopost.env
nano ~/.config/autopost.env
```

Fill in your key and the two account IDs, save (Ctrl+O, Enter), exit
(Ctrl+X). Then lock the file down so only you can read it:

```bash
chmod 600 ~/.config/autopost.env
```

## Step 4 — Make your video folder

```bash
mkdir -p /mnt/ssd/social-queue/captions
```

Don't have an SSD mounted at `/mnt/ssd`? No problem — use any folder you
like (e.g. `/home/pi/social-queue`), just set `AUTOPOST_QUEUE_DIR` to that
path in your config file from Step 3.

## Step 5 — Test it!

Put at least one `.mp4` or `.mov` video in your queue folder, then:

```bash
set -a; source ~/.config/autopost.env; set +a
python3 ~/pi-social-autopost/autopost.py
```

Watch the messages. If everything's set up right, you'll see it upload and
then `Post created:` — go check your TikTok and Instagram. Your Pi just made
its first post. 🎉

If it errors instead, the message will usually tell you exactly what's wrong
(bad key, missing ID, etc.). See Troubleshooting below.

## Step 6 — Put it on a schedule

This uses **systemd timers** — the Pi's built-in alarm clock for programs.
Two small files tell it what to run and when.

First, open `systemd/autopost.service`. The default Raspberry Pi user is
`pi`, so if that's you, it works as-is. If your username is different (run
`whoami` if unsure), replace `pi` in all three spots — the `User=` line and
the two `/home/pi/...` paths. Then:

```bash
cd ~/pi-social-autopost
sudo cp systemd/autopost.service /etc/systemd/system/
sudo cp systemd/autopost.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now autopost.timer
```

By default it posts once a day at 4 PM. Want different times? Edit the timer:

```bash
sudo nano /etc/systemd/system/autopost.timer
```

Each `OnCalendar=` line is one post per day (24-hour clock — `18:15` means
6:15 PM). Add as many lines as you want. After editing:

```bash
sudo systemctl daemon-reload && sudo systemctl restart autopost.timer
```

Check it's armed: `systemctl list-timers | grep autopost` shows the next
run time. That's it — the Pi now posts on its own, and even catches up on a
missed run if it was powered off.

## Step 7 (optional but great) — The web dashboard

Instead of copying files to the Pi by hand, run the little web app and
upload from your phone or laptop browser:

```bash
sudo cp systemd/autopost-uploader.service /etc/systemd/system/
sudo nano /etc/systemd/system/autopost-uploader.service   # replace 'pi' here too if needed (User= and both paths)
sudo systemctl daemon-reload
sudo systemctl enable --now autopost-uploader.service
```

Now open `http://YOUR_PI_IP:5000` from any device on your wifi (find your
Pi's IP with `hostname -I`). You can:

- **Drag and drop videos** straight into the queue
- **Preview any queued video** and edit its caption before it posts
- **See your follower counts** and recent post status
- **Watch your "runway"** — how many days of content you have left

⚠️ The page has no password because it's meant for your home network only.
**Never** forward port 5000 on your router. If you want access away from
home, use something like Tailscale instead.

## How captions work

Two options, and you can mix them:

1. **Per-video:** put a text file next to the video with the same name
   (`My Song.mp4` + `My Song.txt`). Whatever's in the file becomes the
   caption. Easiest way: use the web dashboard's caption editor.
2. **Random pool:** drop a bunch of `.txt` files in the `captions/` folder.
   Any video *without* its own caption file gets a random one from the pool.
   Great for generic captions that fit anything.

A caption file is just plain text — your words plus hashtags. Same caption
goes to both platforms.

## What happens when I run out of videos?

Nothing bad! Once the queue is empty, the Pi starts replaying your old posts
— cycling through every previously posted video once (in random order)
before repeating any of them. Fresh uploads always jump the line. Want it to
go quiet instead? Set `AUTOPOST_RECYCLE=0` in your config file.

## Troubleshooting

**"Missing required env vars"** — your config file isn't being read or is
missing a value. Re-check Step 3.

**"Unauthorized" from Zernio** — the API key is wrong, expired, or has a
typo. Generate a fresh one and update the config file.

**Permission denied errors** — the user running the script doesn't own the
files or the queue folder. Fix with
`sudo chown -R yourname:yourname /path/to/social-queue`.

**It posted but I don't see it on TikTok** — give it a few minutes; TikTok
processes videos after receiving them. If it never shows, check the
`failed/` folder and `autopost.log` in your queue folder for the reason.

**The dashboard shows account stats but not per-post views** — that's
expected: Zernio's API doesn't currently expose per-post analytics, so the
dashboard shows what's real (followers, all-time likes, publish status)
rather than fake numbers.

**Where are the logs?** — `autopost.log` inside your queue folder records
every posting attempt. When in doubt, look there first.

## The moving parts, in one breath

`autopost.py` posts one video (the timer runs it on schedule) ·
`uploader.py` is the web dashboard (runs all the time) · your queue folder
holds videos waiting to post, with `posted/` and `failed/` created
automatically · the config file in `~/.config` holds your secrets.

## License

MIT — do whatever you want with it. If it helps you grow your thing, that's
the whole point.
