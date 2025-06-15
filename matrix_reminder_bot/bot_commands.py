import logging
from datetime import datetime, timedelta, timezone
from typing import List, Optional, Tuple

import arrow
import dateparser
import pytz
from apscheduler.job import Job
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.date import DateTrigger
from apscheduler.triggers.interval import IntervalTrigger
from nio import AsyncClient, MatrixRoom
from nio.events.room_events import RoomMessageText
from cron_descriptor import ExpressionDescriptor, Options, CasingTypeEnum, DescriptionTypeEnum
import humanize
import locale

from matrix_reminder_bot.config import CONFIG
from matrix_reminder_bot.errors import CommandError, CommandSyntaxError
from matrix_reminder_bot.functions import command_syntax, send_text_to_room
from matrix_reminder_bot.reminder import ALARMS, REMINDERS, SCHEDULER, Reminder
from matrix_reminder_bot.storage import Storage

logger = logging.getLogger(__name__)

# Set locale for humanize to Chinese
locale.setlocale(locale.LC_TIME, "zh_CN.UTF-8")
humanize.i18n.activate("zh_CN")


def _get_datetime_now(tz: str) -> datetime:
    """Returns a timezone-aware datetime object of the current time"""
    # Get a datetime with no timezone information
    no_timezone_datetime = datetime.now()

    # Create a datetime.timezone object with the correct offset from UTC
    offset = timezone(pytz.timezone(tz).utcoffset(no_timezone_datetime))

    # Get datetime.now with that offset
    now = datetime.now(offset)

    logger.info("_get_datetime_now: tz %s, now %s", tz, now)

    # Round to the nearest second for nicer display
    return now.replace(microsecond=0)


def _parse_str_to_time(time_str: str, tz_aware: bool = True) -> datetime:
    """Converts a human-readable, future time string to a datetime object

    Args:
        time_str: The time to convert
        tz_aware: Whether the returned datetime should have associated timezone
            information

    Returns:
        datetime: A datetime if conversion was successful

    Raises:
        CommandError: if conversion was not successful, or time is in the past.
    """
    time = dateparser.parse(
        time_str,
        settings={
            "PREFER_DATES_FROM": "future",
            "TIMEZONE": CONFIG.timezone,
            "RETURN_AS_TIMEZONE_AWARE": tz_aware,
        },
    )
    if not time:
        raise CommandError(f"提供的时间 '{time_str}' 无效。")

    logger.info("_parse_str_to_time: %s ➡️ %s", time_str, time)

    # Disallow times in the past
    tzinfo = pytz.timezone(CONFIG.timezone)
    local_time = time
    if not tz_aware:
        local_time = tzinfo.localize(time)
        logger.info("_parse_str_to_time: tz_aware %b, local_time %s", tz_aware, local_time)
    if local_time < _get_datetime_now(CONFIG.timezone):
        raise CommandError(f"提供的时间 '{time_str}' 已经过去，请提供将来的时间。")

    # Round datetime object to the nearest second for nicer display
    time = time.replace(microsecond=0)

    return time


class Command(object):
    def __init__(
        self,
        client: AsyncClient,
        store: Storage,
        command: str,
        room: MatrixRoom,
        event: RoomMessageText,
    ):
        """A command made by a user

        Args:
            client: The client to communicate to matrix with
            store: Bot storage
            command: The command and arguments
            room: The room the command was sent in
            event: The event describing the command
        """
        self.client = client
        self.store = store
        self.room = room
        self.event = event

        msg_without_prefix = command[
            len(CONFIG.command_prefix) :
        ]  # Remove the cmd prefix
        self.args = (
            msg_without_prefix.split()
        )  # Get a list of all items, split by spaces
        self.command = self.args.pop(
            0
        )  # Remove the first item and save as the command (ex. `remindme`)

    def _parse_reminder_command_args_for_cron(self) -> Tuple[str, str]:
        """Processes the list of arguments when a cron tab is present

        Returns:
            A tuple containing the cron tab and the reminder text.
        """

        # Retrieve the cron tab and reminder text

        # Remove "cron" from the argument list
        args = self.args[1:]

        # Combine arguments into a string
        args_str = " ".join(args)
        logger.debug("Parsing cron command arguments: %s", args_str)

        # Split into cron tab and reminder text
        try:
            cron_tab, reminder_text = args_str.split(";", maxsplit=1)
        except ValueError:
            raise CommandSyntaxError()

        return cron_tab, reminder_text.strip()

    def _parse_reminder_command_args(self) -> Tuple[datetime, str, Optional[timedelta]]:
        """Processes the list of arguments and returns parsed reminder information

        Returns:
            A tuple containing the start time of the reminder as a datetime, the reminder text,
            and a timedelta representing how often to repeat the reminder, or None depending on
            whether this is a recurring reminder.

        Raises:
            CommandError: if a time specified in the user command is invalid or in the past
        """
        args_str = " ".join(self.args)
        logger.debug("Parsing command arguments: %s", args_str)

        try:
            time_str, reminder_text = args_str.split(";", maxsplit=1)
        except ValueError:
            raise CommandSyntaxError()
        logger.debug("Got time: %s", time_str)

        # Clean up the input
        time_str = time_str.strip().lower()
        reminder_text = reminder_text.strip()

        # Determine whether this is a recurring command
        # Recurring commands take the form:
        # 每 <recurse time>, <start time>, <text>
        recurring = time_str.startswith("每")
        recurse_timedelta = None
        if recurring:
            # Remove "每" and retrieve the recurse time
            recurse_time_str = time_str[len("每") :].strip()
            logger.debug("Got recurring time: %s", recurse_time_str)

            # Convert the recurse time to a datetime object
            recurse_time = _parse_str_to_time(recurse_time_str)

            # Generate a timedelta between now and the recurring time
            # `recurse_time` is guaranteed to always be in the future
            current_time = _get_datetime_now(CONFIG.timezone)

            recurse_timedelta = recurse_time - current_time
            logger.debug("Recurring timedelta: %s", recurse_timedelta)

            # Extract the start time
            try:
                time_str, reminder_text = reminder_text.split(";", maxsplit=1)
            except ValueError:
                raise CommandSyntaxError()
            reminder_text = reminder_text.strip()

            logger.debug("Start time: %s", time_str)

        # Convert start time string to a datetime object
        time = _parse_str_to_time(time_str, tz_aware=False)

        return time, reminder_text, recurse_timedelta

    async def _confirm_reminder(self, reminder: Reminder):
        """Sends a message to the room confirming the reminder is set

        Args:
            reminder: The Reminder to confirm
        """
        if reminder.cron_tab:
            # Special-case cron-style reminders. We currently don't do any special
            # parsing for them
            await send_text_to_room(
                self.client, self.room.room_id, "好的，我会提醒你！"
            )

            return

        # Convert a datetime to a formatted time (ex. May 25 2020, 01:31)
        start_time = pytz.timezone(reminder.timezone).localize(reminder.start_time)
        human_readable_start_time = start_time.strftime("%Y年%m月%d日 %H:%M")

        # Get a textual representation of who will be notified by this reminder
        target = "你" if reminder.target_user else "房间内所有人"

        # Build the response string
        text = f"好的，我会在 {human_readable_start_time} 提醒{target}"

        if reminder.recurse_timedelta:
            # Inform the user how often their reminder will repeat
            text += f"，之后每{humanize.naturaldelta(reminder.recurse_timedelta, minimum_unit='seconds', months=False)}再次提醒"

        # Add some punctuation
        text += "!"

        if reminder.alarm:
            # Inform the user that an alarm is attached to this reminder
            text += (
                f"\n\n当此提醒触发时，每5分钟会响铃一次，直到被静音。可使用 `{CONFIG.command_prefix}silence` 命令静音。"
            )

        # Send the message to the room
        await send_text_to_room(self.client, self.room.room_id, text)

    async def _remind(self, target: Optional[str] = None, alarm: bool = False):
        """Create a reminder or an alarm with a given target

        Args:
            target: A user ID if this reminder will mention a single user. If None,
                the reminder will mention the whole room
            alarm: Whether this reminder is an alarm. It will fire every 5m after it
                normally fires until silenced.
        """
        # Check whether the time is in human-readable format ("tomorrow at 5pm") or cron-tab
        # format ("* * * * 2,3,4 *"). We differentiate by checking if the time string starts
        # with "cron"
        cron_tab = None
        start_time = None
        recurse_timedelta = None
        if " ".join(self.args).lower().startswith("cron"):
            cron_tab, reminder_text = self._parse_reminder_command_args_for_cron()

            logger.debug(
                "Creating reminder in room %s with cron tab %s: %s",
                self.room.room_id,
                cron_tab,
                reminder_text,
            )
        else:
            (
                start_time,
                reminder_text,
                recurse_timedelta,
            ) = self._parse_reminder_command_args()

            logger.debug(
                "Creating reminder in room %s with delta %s: %s",
                self.room.room_id,
                recurse_timedelta,
                reminder_text,
            )

        if (self.room.room_id, reminder_text.upper()) in REMINDERS:
            await send_text_to_room(
                self.client,
                self.room.room_id,
                "已经存在相同的提醒内容的提醒了，请先删除原有提醒。",
            )
            return

        # Create the reminder
        reminder = Reminder(
            self.client,
            self.store,
            self.room.room_id,
            reminder_text,
            start_time=start_time,
            timezone=CONFIG.timezone,
            cron_tab=cron_tab,
            recurse_timedelta=recurse_timedelta,
            target_user=target,
            alarm=alarm,
        )

        # Record the reminder
        REMINDERS[(self.room.room_id, reminder_text.upper())] = reminder
        self.store.store_reminder(reminder)

        # Send a message to the room confirming the creation of the reminder
        await self._confirm_reminder(reminder)

    async def process(self):
        """Process the command"""
        if self.command in ["remindme", "remind", "r"]:
            await self._remind_me()
        elif self.command in ["remindroom", "rr"]:
            await self._remind_room()
        elif self.command in ["alarmme", "alarm", "a"]:
            await self._alarm_me()
        elif self.command in ["alarmroom", "ar"]:
            await self._alarm_room()
        elif self.command in ["listreminders", "listalarms", "list", "lr", "la", "l"]:
            await self._list_reminders()
        elif self.command in [
            "delreminder",
            "deletereminder",
            "removereminder",
            "cancelreminder",
            "delalarm",
            "deletealarm",
            "removealarm",
            "cancelalarm",
            "cancel",
            "rm",
            "cr",
            "ca",
            "d",
            "c",
        ]:
            await self._delete_reminder()
        elif self.command in ["silence", "s"]:
            await self._silence()
        elif self.command in ["help", "h"]:
            await self._help()

    @command_syntax("[每 <循环时间>;] <开始时间>; <提醒内容>")
    async def _remind_me(self):
        """Set a reminder that will remind only the user who created it"""
        await self._remind(target=self.event.sender)

    @command_syntax("[每 <循环时间>;] <开始时间>; <提醒内容>")
    async def _remind_room(self):
        """Set a reminder that will mention the room that the reminder was created in"""
        await self._remind()

    @command_syntax("[每 <循环时间>;] <开始时间>; <提醒内容>")
    async def _alarm_me(self):
        """Set a reminder with an alarm that will remind only the user who created it"""
        await self._remind(target=self.event.sender, alarm=True)

    @command_syntax("[每 <循环时间>;] <开始时间>; <提醒内容>")
    async def _alarm_room(self):
        """Set a reminder with an alarm that when fired will mention the room that the
        reminder was created in
        """
        await self._remind(alarm=True)

    @command_syntax("[<提醒内容>]")
    async def _silence(self):
        """Silences an ongoing alarm"""

        # Attempt to find a reminder with an alarm currently going off
        reminder_text = " ".join(self.args)
        if reminder_text:
            # Find the alarm job via its reminder text
            alarm_job = ALARMS.get((self.room.room_id, reminder_text.upper())).alarm_job

            if alarm_job:
                await self._remove_and_silence_alarm(alarm_job, reminder_text)
                text = f"闹钟 '{reminder_text}' 已静音。"
            else:
                # We didn't find an alarm with that reminder text
                #
                # Be helpful and check if this is a known reminder without an alarm
                # currently going off
                reminder = REMINDERS.get((self.room.room_id, reminder_text.upper()))
                if reminder:
                    text = (
                        f"提醒 '{reminder_text}' 当前没有闹钟正在响铃。"
                    )
                else:
                    # Nope, can't find it
                    text = f"未知的闹钟或提醒 '{reminder_text}'。"
        else:
            # No reminder text provided. Check if there's a reminder currently firing
            # in the room instead then
            for alarm_info, reminder in ALARMS.items():
                if alarm_info[0] == self.room.room_id:
                    # Found one!
                    reminder_text = alarm_info[
                        1
                    ].capitalize()  # normalize the text a bit

                    await self._remove_and_silence_alarm(
                        reminder.alarm_job, reminder_text
                    )
                    text = f"闹钟 '{reminder_text}' 已静音。"

                    # Prevent the `else` clause from being triggered
                    break
            else:
                # If we didn't find any alarms...
                text = "当前房间没有正在响铃的闹钟。"

        await send_text_to_room(self.client, self.room.room_id, text)

    async def _remove_and_silence_alarm(self, alarm_job: Job, reminder_text: str):
        # We found a reminder with an alarm. Remove it from the dict of current
        # alarms
        ALARMS.pop((self.room.room_id, reminder_text.upper()), None)

        if SCHEDULER.get_job(alarm_job.id):
            # Silence the alarm job
            alarm_job.remove()

    @command_syntax("")
    async def _list_reminders(self):
        """Format and show known reminders for the current room

        Sends a message listing them in the following format, using the alarm clock emoji ⏰ to indicate an alarm:

            ⏰ Firing Alarms

            * [🔁 every <recurring time>;] <start time>; <reminder text>

            1️⃣ One-time Reminders

            * [⏰] <start time>: <reminder text>

            📅 Cron Reminders

            * [⏰] m h d M wd (`m h d M wd`); next run in <rounded next time>; <reminder text>

            🔁 Repeating Reminders

            * [⏰] every <recurring time>; next run in <rounded next time>; <reminder text>

        or if there are no reminders set:

            There are no reminders for this room.
        """
        output = ""

        cron_reminder_lines: List = []
        one_shot_reminder_lines: List = []
        interval_reminder_lines: List = []
        firing_alarms_lines: List = []

        for alarm in ALARMS.values():
            line = "- "
            if isinstance(alarm.job.trigger, IntervalTrigger):
                line += f"🔁 每{humanize.naturaldelta(alarm.recurse_timedelta, minimum_unit='seconds', months=False)}; "
            line += f'"*{alarm.reminder_text}*"'
            firing_alarms_lines.append(line)

        # Sort the reminder types
        for reminder in REMINDERS.values():
            # Filter out reminders that don't belong to this room
            if reminder.room_id != self.room.room_id:
                continue

            # Organise alarms into markdown lists
            line = "- "
            if reminder.alarm:
                # Note that an alarm exists if available
                alarm_clock_emoji = "⏰"
                line += alarm_clock_emoji + " "

            # Print the duration before (next) execution
            next_execution = reminder.job.next_run_time
            next_execution = arrow.get(next_execution)
            # One-time reminders
            if isinstance(reminder.job.trigger, DateTrigger):
                # Just print when the reminder will go off
                line += f"{next_execution.format('YYYY年MM月DD日 HH:mm')}"

            # Repeat reminders
            elif isinstance(reminder.job.trigger, IntervalTrigger):
                # Print the interval, and when it will next go off
                line += f"每{humanize.naturaldelta(reminder.recurse_timedelta, minimum_unit='seconds', months=False)}; 下次提醒 {next_execution.humanize(locale='zh_cn')}"

            # Cron-based reminders
            elif isinstance(reminder.job.trigger, CronTrigger):
                # A human-readable cron tab, in addition to the actual tab
                options = Options()
                options.locale_code = "zh_CN"
                options.casing_type = CasingTypeEnum.Sentence
                options.use_24hour_time_format = True
                descriptor = ExpressionDescriptor(reminder.cron_tab, options)
                human_cron = descriptor.get_description(DescriptionTypeEnum.FULL)
                if human_cron != reminder.cron_tab:
                    line += f"{human_cron} (`{reminder.cron_tab}`)"
                else:
                    line += f"`每 {reminder.cron_tab}`"
                line += f"; 下次提醒 {next_execution.humanize(locale='zh_cn')}"

            # Add the reminder's text
            line += f'; *"{reminder.reminder_text}"*'

            # Output the status of each reminder. We divide up the reminders by type in order
            # to show them in separate sections, and display them differently
            if isinstance(reminder.job.trigger, DateTrigger):
                one_shot_reminder_lines.append(line)
            elif isinstance(reminder.job.trigger, IntervalTrigger):
                interval_reminder_lines.append(line)
            elif isinstance(reminder.job.trigger, CronTrigger):
                cron_reminder_lines.append(line)

        if (
            not firing_alarms_lines
            and not one_shot_reminder_lines
            and not interval_reminder_lines
            and not cron_reminder_lines
        ):
            await send_text_to_room(
                self.client,
                self.room.room_id,
                "*当前房间没有设置任何提醒。*",
            )
            return

        if firing_alarms_lines:
            output += "\n\n" + "**⏰ 正在响铃的闹钟**" + "\n\n"
            output += "\n".join(firing_alarms_lines)

        if one_shot_reminder_lines:
            output += "\n\n" + "**1️⃣ 一次性提醒**" + "\n\n"
            output += "\n".join(one_shot_reminder_lines)

        if interval_reminder_lines:
            output += "\n\n" + "**🔁 循环提醒**" + "\n\n"
            output += "\n".join(interval_reminder_lines)

        if cron_reminder_lines:
            output += "\n\n" + "**📅 Cron提醒**" + "\n\n"
            output += "\n".join(cron_reminder_lines)

        await send_text_to_room(self.client, self.room.room_id, output)

    @command_syntax("<提醒内容>")
    async def _delete_reminder(self):
        """Delete a reminder via its reminder text"""
        reminder_text = " ".join(self.args)
        if not reminder_text:
            raise CommandSyntaxError()

        logger.debug("Known reminders: %s", REMINDERS)
        logger.debug(
            "Deleting reminder in room %s: %s", self.room.room_id, reminder_text
        )

        reminder = REMINDERS.get((self.room.room_id, reminder_text.upper()))
        if reminder:
            # Cancel the reminder and associated alarms
            reminder.cancel()

            text = "提醒"
            if reminder.alarm:
                text = "闹钟"
            text += f' "*{reminder_text}*" 已取消。'
        else:
            text = f"未知的提醒 '{reminder_text}'。"

        await send_text_to_room(self.client, self.room.room_id, text)

    @command_syntax("")
    async def _help(self):
        """Show the help text"""
        # Ensure we don't tell the user to use something other than their configured command
        # prefix
        c = CONFIG.command_prefix

        if not self.args:
            text = (
                f"你好，我是提醒小助手！使用 `{c}help reminders` 查看可用命令。"
            )
            await send_text_to_room(self.client, self.room.room_id, text)
            return

        topic = self.args[0]

        # Simply way to check for plurals
        if topic.startswith("reminder"):
            text = f"""
**提醒小助手的使用方法**

视频教程：https://words6000.sharepoint.com/:f:/s/public/EsZPnUswl0tKuDuC0SqMCokBJo44PA1HbukLlwh7_acymw

注意：`!` `;` 是英文的感叹号和分号，不要使用中文的感叹号和分号

**1.创建一次性的提醒，只@创建者自己：**

```
{c}remindme 今天19:00 ; 提醒我填写CD链接 https://www.baidu.com
```

`remindme` 可以简写为 `r`:

```
{c}r 今天19:00 ; 提醒我填写CD链接 https://www.baidu.com
{c}r 明天05:00 ; 提醒我填写CD链接 https://www.baidu.com
{c}r 本周日05:00 ; 提醒我填写CD链接 https://www.baidu.com
{c}r 2025年7月7日12:00 ; 提醒我填写CD链接 https://www.baidu.com
```

**2.创建一次性的提醒，@房间内所有人（需要小助手有@整个房间的权限）：**

把上面命令中 `remindme` 改为 `remindroom` 或 `rr` 即可:

```
{c}remindroom 今天19:00 ; 提醒我填写CD链接 https://www.baidu.com
{c}rr 今天19:00 ; 提醒我填写CD链接 https://www.baidu.com
```

**3.创建每周的循环提醒**

只@创建者自己:

```
{c}r 每1周; 周一05:10; 每周一早上5点参加JSJY
```

@所有人:

```
{c}rr 每1周; 周一05:10; 每周一早上5点参加JSJY
```

**4.列出房间内所有提醒：**

```
{c}list
```

或者简写为：

```
{c}l
```

**5.取消提醒或闹钟：**

```
{c}cancel 完整的提醒内容
```

或者简写为：

```
{c}c 完整的提醒内容
```

**6.创建一个在到点后每5分钟响铃一次的提醒（闹钟）：**

只@创建者自己:

```
{c}alarmme 其他命令格式和提醒一样
```

或者简写为：

```
{c}a 其他命令格式和提醒一样
```

@房间内所有人:

```
{c}alarmroom 其他命令格式和提醒一样
```

或者简写为：

```
{c}ar 其他命令格式和提醒一样
```

闹钟响铃后可用以下命令静音：

```
{c}silence 完整的提醒内容
```

或者简写为：

```
{c}s [<提醒内容>]
```

**7.高级Cron语法**

用 `cron表达式` 实现更灵活任意的提醒功能，请使用 AI工具 帮你生成 `cron表达式`，参考提示词：`请帮我生成一个cron表达式，要求：每月18号早上6点钟`，然后使用以下命令：

```
{c}r cron cron表达式; 提醒内容
{c}rr cron cron表达式; 提醒内容
{c}a cron cron表达式; 提醒内容
{c}ar cron cron表达式; 提醒内容
```

例如 每月18号早上6点钟: `{c}rr cron 0 6 18 * *; 提醒内容`
"""
        else:
            # Unknown help topic
            return

        await send_text_to_room(self.client, self.room.room_id, text)

    async def _unknown_command(self):
        """Computer says 'no'."""
        await send_text_to_room(
            self.client,
            self.room.room_id,
            f"未知帮助主题 '{self.command}'。请尝试使用 'help' 命令获取更多信息。",
        )
