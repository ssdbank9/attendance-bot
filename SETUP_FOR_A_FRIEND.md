# Setting this up on your laptop

This puts a small helper on your Windows laptop. Each morning and each evening it
marks your attendance for you and sends your phone a message to say it did.
Everything stays on your laptop: no account to make, nothing to sign up for, and
no copy of your details kept anywhere else.

## Before you start (about 10 minutes)

1. **Microsoft Edge**, the browser that comes with Windows. You already have it.
2. **Python**, the language the helper is written in. Go to
   https://www.python.org/downloads/ and click the big download button. When the
   installer opens, **tick the box at the bottom saying "Add python.exe to PATH"**
   before clicking Install. That tick matters; ignore everything else.
3. Put the bot folder somewhere it can stay forever, for example `C:\Attendance`.
   Do not move or rename it later - the daily schedule remembers where it is.
4. You must be on the work (AKU) network when the helper runs. Away from that
   network it simply cannot reach the attendance system.

## The one command to run

A "terminal" is just a window where you type instructions instead of clicking.

1. Click Start and type `powershell`.
2. Right-click **Windows PowerShell**, choose **Run as administrator**, and say
   Yes to the popup. (Administrator means "allowed to set up daily tasks".)
3. Type these two lines, pressing Enter after each. Change the folder name if you
   put it somewhere other than `C:\Attendance`.

```
cd C:\Attendance
python setup_new_user.py
```

Then leave it alone until it prints `SETUP COMPLETE!`. That is the whole job.

## What it asks you, in plain words

- **Employee/User ID** - the ID you normally type into the attendance page.
- **Password** - the password for that same attendance page.
- **Portal username** - your one.aku.edu sign-in name, usually
  `firstname.lastname`. Only used as a backup route if the main one is down.
- **Portal password** - the password for one.aku.edu.
- **Time-In window start and end** - the earliest and latest clock time you are
  happy to be marked present. It picks a random minute in between, so it is never
  the same time two days running. Press Enter to accept 08:45 to 09:05.
- **Time-Out window start and end** - same idea for leaving. Press Enter for
  20:00 to 21:30.

At the end it prints a **ntfy topic**: a private channel name that looks like
`timeinbot-12345-ab7cd`. Keep it to yourself - anyone who knows it can read your
notifications.

## Getting the messages on your phone

Install the free app **ntfy** from the App Store or Google Play, open it, tap
**+**, and type that topic name exactly as printed. Your phone now buzzes every
time attendance is marked.

## Opening the control page from your phone

The control page (the "dashboard") is where you check status or book a sick day.

1. On the laptop, in that same PowerShell window, type `ipconfig` and press
   Enter. Look for **IPv4 Address** - four numbers like `192.168.1.24`.
2. Put the phone on the same Wi-Fi as the laptop.
3. In the phone browser go to `http://192.168.1.24:5000`, using your own numbers.
4. It asks for a login code. On the laptop open the bot folder, open the file
   named `.dashboard_auth_token` with Notepad, and copy that long line of letters
   into the box on your phone. You do this once per phone.

## Checking it worked the next morning

Any time after your Time-In window closes (09:05 unless you changed it), do any
one of these:

- Look at your phone. A message should have arrived.
- Open the control page - today shows as done at the top.
- On the laptop, open `timein_status.json` in the bot folder with Notepad. If it
  mentions today's date and the word success, it worked.

If nothing happened, the laptop was switched off, asleep, or off the work
network. It does try to wake itself, but on many modern laptops Windows ignores
that, so leaving the lid open is the reliable option.

When it does miss the window, it deliberately marks nothing rather than putting
a silly time like 1 PM against your name. Instead your phone gets a "window
missed" message with a button on it - tap that button and it marks you straight
away.

The helper never draws anything on your screen, on purpose. Seeing nothing
happen during the day is the normal, correct behaviour.
