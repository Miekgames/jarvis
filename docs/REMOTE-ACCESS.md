# Reaching JARVIS from your phone

JARVIS runs on one machine. Reaching it from another is three separate
changes, and each is useless or unsafe without the others. Two scripts do the
work:

```
python scripts/remote_access.py status    what would work right now
python scripts/remote_access.py on        open it up (asks for a password)
python scripts/autostart.py status        will it be running at all?
```

---

## What has to be true

### 1. It has to be running

JARVIS is a program someone started, not a service. Close the terminal and it
stops — and an assistant you can reach only while you are sitting at the
machine is not worth reaching.

```
python scripts/autostart.py install
```

This puts a shortcut in your own Startup folder. Deliberately not a service or
a registry key: it needs no administrator rights, Windows lists it under Task
Manager → Startup apps so you can see it and switch it off, and removing it is
deleting one file (`python scripts/autostart.py uninstall`).

It starts the backend only. The backend serves the built interface from
`frontend/dist` on the same origin, so there is no second dev-server process —
one Python process, not a Node toolchain.

It then **stays in front of that process and restarts it if it dies**. A thing
you reach from your phone has to survive a crash at three in the morning
without someone walking over to the desk. Restarts back off, and a process
that keeps dying immediately is left dead with a line in
`data/logs/autostart.log` rather than hammered forever.

### 2. It has to listen beyond this machine

By default JARVIS binds to `127.0.0.1`, which no other device can reach. That
is the right default. Changing it is `JARVIS_HOST=0.0.0.0`.

### 3. It has to let another machine log in

This is the one that surprises people. `JARVIS_AUTH_MODE=local` does not
merely *skip* the password — it actively **refuses any request that did not
come from this machine**, and refuses to let it log in either. Opening the
port without also switching to `password` mode gets you a phone that connects
and is then turned away.

So `password` mode, and a password you choose. `remote_access.py on` will not
invent one for you and will not accept a short one.

---

## Getting there

```
python scripts/remote_access.py on
```

It asks for a password (not echoed, not left in your shell history), writes
the three settings to `.env`, and backs the old `.env` up to `backups/` first
— that file holds API keys, and rewriting it without a copy would be an
unkind way to lose them.

Then restart JARVIS and open the address it prints.

---

## Do not forward a port on your router

A machine that can read your files, run commands and hold your credentials
should not be answering the open internet, password or no password.

Use [Tailscale](https://tailscale.com) instead. It gives your devices their
own private addresses that only they can reach; nothing is exposed publicly,
and there is nothing to configure on the router. Install it on the computer
and on your phone, log both into the same account, and the computer gets a
stable `100.x.y.z` address that works from anywhere.

`remote_access.py status` prints that address when Tailscale is connected.

---

## The firewall caveat

`JARVIS_HOST=0.0.0.0` means JARVIS listens on *every* network interface, not
only the Tailscale one — so on whatever wifi you happen to join, the port is
there too, with only the password in front of it.

```
python scripts/remote_access.py firewall
```

adds an inbound rule limiting the port to Tailscale's address range. It needs
administrator rights, which is why it is a deliberate separate step.

**It can be defeated by a rule you already have.** Windows Firewall permits a
packet if *any* enabled Allow rule matches it, so a tailnet-scoped rule
restricts nothing while a broader rule already covers the same port. The usual
culprit: the first time you ran a Python server, Windows asked "allow
python.exe on your networks?" and a yes left a rule allowing **every port from
every address** for the life of that interpreter.

`remote_access.py status` looks for exactly these and names them. If it says
it could not check, believe that rather than assuming the port is limited —
the difference between "nothing else allows this" and "we could not tell" is
the difference between a true claim and a false one.

---

## What it looks like from the phone

The full interface, phone-shaped: the particle field, the command bar at the
bottom, and the sidebar behind a button in the top right rather than pinned to
the edge. Everything works — chat, tasks, panels, live updates.

The one thing that stays on the computer is the **wake word**. "Hey JARVIS"
listens through the computer's microphone; the phone is a window onto the same
assistant, not a second pair of ears.

---

## When it does not work

**Black or blank page on the phone.** Almost always nothing listening at that
address. Run `remote_access.py status` on the computer: if the bind address is
`127.0.0.1`, the phone's connection is being refused and the browser is
showing you its own empty error page.

**It loads, then says OFFLINE / LINK LOST.** The page is served but the live
event stream is not connecting. Check that you are on a build from after the
websocket started reading the session cookie — before that, the event stream
accepted a token only from the query string, which a browser cannot supply
because the session cookie is deliberately unreadable from script.

**It asks for a password and will not take it.** Check `JARVIS_AUTH_MODE` is
actually `password` and that JARVIS was restarted after the change. In `local`
mode the login endpoint itself refuses anything that is not this machine, so a
correct password is still rejected.

**Nothing is running at all.** `python scripts/autostart.py logs` shows what
happened at logon, and `data/logs/backend.log` has the backend's own output.
