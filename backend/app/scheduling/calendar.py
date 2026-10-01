"""工作行事曆：排程引擎只看「工作分鐘數」（從排程起點起算、只計上班時間），這裡負責和實際時間互換。

例如每天 08:00–12:00、13:00–17:00 兩個班次（480 分鐘），週六日與國定假日不上班：
第 0 分鐘＝第一個工作日 08:00，第 480 分鐘＝第二個工作日 08:00。加工跨午休、跨夜、跨週末時，
引擎不必知道，換算成實際時間時自然會跳過。委外天數也以工作天計（1 天＝480 工作分鐘）。
"""

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta


def _hm(s: str) -> time:
    h, m = s.split(":")
    return time(int(h), int(m))


def _mins(t: time) -> int:
    return t.hour * 60 + t.minute


@dataclass(frozen=True)
class WorkDay:
    day: date
    start_min: int  # 這天第一個班次在工作分鐘軸上的位置


class WorkCalendar:
    def __init__(
        self,
        start: date,
        shifts: list[tuple[str, str]],
        workdays: list[int],
        holidays: dict[str, str] | None = None,
        horizon_days: int = 400,
    ):
        self.shifts = [(_hm(a), _hm(b)) for a, b in shifts]
        self.day_minutes = sum(_mins(b) - _mins(a) for a, b in self.shifts)
        self.holidays = {date.fromisoformat(d): name for d, name in (holidays or {}).items()}
        self.workdays = set(workdays)
        self.days: list[WorkDay] = []
        d = start
        while len(self.days) < horizon_days:
            if d.isoweekday() in self.workdays and d not in self.holidays:
                self.days.append(WorkDay(d, len(self.days) * self.day_minutes))
            d += timedelta(days=1)
        self._index = {wd.day: i for i, wd in enumerate(self.days)}

    @property
    def start(self) -> date:
        return self.days[0].day

    def to_datetime(self, minute: int, is_end: bool = False) -> datetime:
        """工作分鐘 → 實際時間。is_end=True 時，剛好落在班次交界的時間算前一個班次的結束
        （例如第 480 分鐘的結束時間是第一天 17:00，而不是第二天 08:00）。"""
        minute = max(0, int(minute))
        i, rem = divmod(minute, self.day_minutes)
        if is_end and rem == 0 and minute > 0:
            i, rem = i - 1, self.day_minutes
        i = min(i, len(self.days) - 1)
        for a, b in self.shifts:
            length = _mins(b) - _mins(a)
            if rem < length or (is_end and rem == length):
                return datetime.combine(self.days[i].day, a) + timedelta(minutes=rem)
            rem -= length
        return datetime.combine(self.days[i].day, self.shifts[-1][1])

    def from_datetime(self, dt: datetime) -> int:
        """實際時間 → 工作分鐘；落在下班時間就取下一個上班時刻（排程起點之前為 0）。"""
        d = dt.date()
        if d < self.start:
            return 0
        while d not in self._index:
            d += timedelta(days=1)
            if d > self.days[-1].day:
                return self.days[-1].start_min + self.day_minutes
            dt = datetime.combine(d, time(0, 0))
        base = self.days[self._index[d]].start_min
        t = _mins(dt.time())
        acc = 0
        for a, b in self.shifts:
            if t <= _mins(a):
                return base + acc
            if t < _mins(b):
                return base + acc + t - _mins(a)
            acc += _mins(b) - _mins(a)
        return base + self.day_minutes

    def end_of_day(self, d: date) -> int:
        """這天（或之前最後一個工作日）下班時的工作分鐘：交期當天下班前完工都算準時。"""
        if d < self.start:
            return 0
        while d not in self._index:
            d -= timedelta(days=1)
            if d < self.start:
                return 0
        return self.days[self._index[d]].start_min + self.day_minutes

    def start_of_day(self, d: date) -> int:
        """這天（或之後第一個工作日）上班時的工作分鐘。"""
        return self.from_datetime(datetime.combine(d, time(0, 0)))

    def axis(self, until_minute: int) -> list[dict]:
        """前端甘特圖的日期刻度：每個工作日一格，標出假日。"""
        n = min(len(self.days), until_minute // self.day_minutes + 1)
        out = []
        for i, wd in enumerate(self.days[:n]):
            gap = [
                {"date": str(d), "name": self.holidays[d]}
                for d in self.holidays
                if (self.days[i - 1].day if i else self.start) < d < wd.day
            ]
            out.append(
                {
                    "date": str(wd.day),
                    "weekday": "一二三四五六日"[wd.day.isoweekday() - 1],
                    "start_min": wd.start_min,
                    "holidays_before": gap,
                }
            )
        return out
