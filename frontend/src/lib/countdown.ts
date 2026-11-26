/**
 * How long is left, in months, days, hours, minutes and seconds.
 *
 * Months are calendar months: from 22 November to 18 December is not "26/30 of
 * a month", it is 0 months and 25-and-a-bit days, and to 22 January it is
 * exactly two. Months are the largest unit, so an event a year and a half
 * away reads 18 months.
 *
 * The arithmetic is done on wall-clock values. An event "on the 18th at
 * 19:30" is at 19:30 whatever daylight saving does in between.
 */

export interface Countdown {
  id: string;
  title: string;
  topic: string;
  /** YYYY-MM-DD */
  day: string;
  /** HH:MM, or '' for the start of the day */
  clock: string;
  origin: 'owner' | 'web';
  source_name: string;
  source_url: string;
  sources: number;
  checked_at: string | null;
}

export interface Left {
  months: number;
  days: number;
  hours: number;
  minutes: number;
  seconds: number;
}

export interface Parts { year: number; month: number; day: number; hour: number; minute: number; second: number }

const wall = (p: Parts) => Date.UTC(p.year, p.month, p.day, p.hour, p.minute, p.second);

export function partsOf(date: Date): Parts {
  return {
    year: date.getFullYear(), month: date.getMonth(), day: date.getDate(),
    hour: date.getHours(), minute: date.getMinutes(), second: date.getSeconds(),
  };
}

export function targetParts(item: Pick<Countdown, 'day' | 'clock'>): Parts {
  const [year, month, day] = item.day.split('-').map(Number);
  const [hour, minute] = item.clock ? item.clock.split(':').map(Number) : [0, 0];
  return { year, month: month - 1, day, hour, minute, second: 0 };
}

/** The same time of day, some months on; the 31st becomes the month's last day. */
function monthsOn(from: Parts, months: number): Parts {
  const index = from.year * 12 + from.month + months;
  const year = Math.floor(index / 12);
  const month = index % 12;
  const last = new Date(Date.UTC(year, month + 1, 0)).getUTCDate();
  return { ...from, year, month, day: Math.min(from.day, last) };
}

/** What is left, or null once the moment has come. */
export function left(now: Parts, target: Parts): Left | null {
  const end = wall(target);
  if (end <= wall(now)) return null;
  let months = (target.year - now.year) * 12 + (target.month - now.month);
  while (months > 0 && wall(monthsOn(now, months)) > end) months -= 1;
  const total = Math.floor((end - wall(monthsOn(now, months))) / 1000);
  return {
    months,
    days: Math.floor(total / 86400),
    hours: Math.floor((total % 86400) / 3600),
    minutes: Math.floor((total % 3600) / 60),
    seconds: total % 60,
  };
}

/** Days since the event's day: 0 today, 1 yesterday. */
export function daysSince(now: Parts, target: Parts): number {
  const from = Date.UTC(target.year, target.month, target.day);
  const to = Date.UTC(now.year, now.month, now.day);
  return Math.max(0, Math.round((to - from) / 86400000));
}

/** Still to come first, soonest first; then what has been. */
export function inOrder<T extends Pick<Countdown, 'day' | 'clock'>>(items: T[], now: Parts): T[] {
  const here = wall(now);
  return [...items].sort((a, b) => {
    const [x, y] = [wall(targetParts(a)), wall(targetParts(b))];
    const [xDone, yDone] = [x <= here, y <= here];
    if (xDone !== yDone) return xDone ? 1 : -1;
    return x - y;
  });
}

/** "3 min ago", "2 days ago", "18 Nov" -- for news. */
export function when(iso: string, now: Date = new Date()): string {
  const then = new Date(iso);
  if (!iso || Number.isNaN(then.getTime())) return '';
  const minutes = Math.round((now.getTime() - then.getTime()) / 60000);
  if (minutes < 1) return 'just now';
  if (minutes < 60) return `${minutes} min ago`;
  const hours = Math.round(minutes / 60);
  if (hours < 24) return `${hours} h ago`;
  const days = Math.round(hours / 24);
  if (days === 1) return 'yesterday';
  if (days < 14) return `${days} days ago`;
  return then.toLocaleDateString([], { day: 'numeric', month: 'short', year: 'numeric' });
}

export function spoken(item: Countdown, l: Left | null): string {
  if (!l) return `${item.title}: it is here.`;
  const units: [number, string][] = [[l.months, 'month'], [l.days, 'day'], [l.hours, 'hour'], [l.minutes, 'minute']];
  const first = units.findIndex(([n]) => n > 0);
  if (first === -1) return `${item.title}: less than a minute left.`;
  const said = units.slice(first, first + 3).filter(([n]) => n > 0)
    .map(([n, name]) => `${n} ${name}${n === 1 ? '' : 's'}`).join(', ');
  return `${item.title}: ${said} left.`;
}
