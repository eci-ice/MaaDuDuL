"""糖果自由（棉花糖）自动刷取相关自定义动作模块。

自动模式按三种优质棉花糖的持有数量，选择「两两之和最小」的组合关卡，
并把关卡定位到最新的三星关（列表滑到最底后，从右下往左上找第一个三星）。

Note:
    - 数量来源是关卡列表右侧「奖励列表」：逐个点开图标读名称与所持数量，再点一次关闭；
      列表里会混排中级棉花糖等档位，只统计名称带「优质」的那些；
    - 一关只会出现该关的两种棉花糖，因此要进两关才能凑齐三种；
    - 金星与罐内浅黄糖同色，只能先按星条底色定位每一格，再在条内按三等分逐格数金星。
"""

import numpy as np
from maa.agent.agent_server import AgentServer
from maa.context import Context
from maa.custom_action import CustomAction

from agent.customs.maahelper import RecoHelper, Tasker
from agent.customs.utils import Prompter

# 奖励列表里还有中级等其它档位，只有优质棉花糖才参与统计
CANDY_KEYWORD = "优质"

# 三种优质棉花糖的名称关键字，以及 OCR 常见误读的容错写法
COTTON_NAMES = ("防御", "输出", "辅助")
COTTON_ALIASES = {
    "方御": "防御",
    "防卸": "防御",
    "俞出": "输出",
    "输山": "输出",
    "辅肋": "辅助",
}

# 三个机器的入口坐标与产物组合（1280×720 基准）
# 红：不能选择防御职能 → 产物 辅助 + 输出
# 蓝：不能选择输出职能 → 产物 防御 + 辅助
# 绿：不能选择辅助职能 → 产物 防御 + 输出
MACHINES = {
    "输出/辅助": {"point": (223, 347), "candies": ("输出", "辅助")},
    "防御/辅助": {"point": (640, 347), "candies": ("防御", "辅助")},
    "防御/输出": {"point": (1053, 347), "candies": ("防御", "输出")},
}
# 取数只需进两个机器即可覆盖三种糖：蓝（防御+辅助）与绿（防御+输出）
COUNT_MACHINES = ("防御/辅助", "防御/输出")

# 关卡列表右侧奖励图标（1280 基准）；最右侧一个会被裁切，不取
ICON_POS = ((813, 343), (913, 343), (1013, 343), (1107, 343))
NAME_NODE = "糖果自由_识别奖励名称"
COUNT_NODE = "糖果自由_识别奖励数量"

# 关卡网格（1280 基准）：滑到底后可见三行三列，格子的位置随滚动变化，所以不写死坐标，
# 而是用每一格星条底色 (199,147,72) 找到星条，再按三等分逐格数金星。
# 金星 (255,183,31) 与罐内浅黄糖同色，只能框在星条内部数；实测一颗金星约占 620 像素，
# 空格子为 0 像素，两者差距足够大。
BAR_COLOR = (199, 147, 72)
BAR_TOLERANCE = 12
BAR_PANE_RIGHT = 730
BAR_ROW_GAP = 30
BAR_CELL_GAP = 60
BAR_MIN_ROW_PIXELS = 100
BAR_MIN_COLUMN_PIXELS = 3
BAR_WIDTH = (100, 220)
BAR_INSET = 6
BAR_STAR_PIXELS = 150

# 返回糖果自由页的返回键、滑动到底的起止点与最大次数
BACK_POINT = (43, 37)
SCROLL_FROM = (360, 560)
SCROLL_TO = (360, 220)
SCROLL_MAX = 8


def match_candy(name: str) -> str:
    """从奖励名称里认出是哪种棉花糖。

    容忍 OCR 把「防御」读成「方御」这类误读。

    Args:
        name：OCR 读到的名称文本

    Returns:
        str：棉花糖类型，认不出时返回空串
    """
    name = name or ""
    for candy in COTTON_NAMES:
        if candy in name:
            return candy
    for wrong, right in COTTON_ALIASES.items():
        if wrong in name:
            return right
    return ""


def read_reward(context: Context, point: tuple[int, int]) -> tuple[str, int]:
    """点开一个奖励图标读名称与所持数量，读完再点一次关掉面板。

    奖励列表里除了棉花糖还会有晶体、绷带这类东西，点它们不弹名称面板，
    这时识别不到任何文本，所以两种 OCR 结果都要按「可能为空」处理；
    面板没弹出来时也不能再补一次点击，否则会把下一个图标的面板点开。

    Args:
        context：MaaFW 上下文对象
        point：奖励图标坐标（1280 基准）

    Returns:
        tuple：名称与数量；不是棉花糖或没弹面板时数量为 -1
    """
    tasker = Tasker(context)
    tasker.click(*point).wait()
    rh = RecoHelper(context)
    opened = False
    for _ in range(3):  # 面板有弹出动画，识别不到就再抓一帧重试
        rh.refresh_screencap().recognize(NAME_NODE)
        if rh.hit:
            opened = True
            break
        tasker.wait(0.4)
    name = rh.concat() or ""
    value = -1
    if opened and CANDY_KEYWORD in name and match_candy(name):
        rh.recognize(COUNT_NODE)
        digits = "".join(ch for ch in (rh.concat() or "") if ch.isdigit())
        value = int(digits) if digits else -1
    if opened:
        tasker.click(*point).wait()  # 再点一次同一个图标即关闭面板
        tasker.wait(0.3)
    return name, value


def choose_combo(counts: dict[str, int]) -> str:
    """选出要刷的组合：持有最多的那种不用补，刷另外两种。

    Args:
        counts：三种棉花糖的持有数量

    Returns:
        str：组合名，取值属于 MACHINES；数量不全时返回空串
    """
    if any(candy not in counts for candy in COTTON_NAMES):
        return ""
    most = max(COTTON_NAMES, key=lambda candy: counts[candy])
    for combo, info in MACHINES.items():
        if most not in info["candies"]:
            return combo
    return ""


def group_runs(indexes: np.ndarray, gap: int) -> list[tuple[int, int]]:
    """把一维下标按「相邻间隔不超过 gap」合并成若干连续区间。

    Args:
        indexes：升序下标
        gap：允许的最大间隔

    Returns:
        list：[(起点, 终点), ...]
    """
    runs: list[tuple[int, int]] = []
    start = previous = int(indexes[0])
    for value in indexes[1:]:
        value = int(value)
        if value - previous > gap:
            runs.append((start, previous))
            start = value
        previous = value
    runs.append((start, previous))
    return runs


def read_grid(shot: np.ndarray) -> list[tuple[int, int, int]]:
    """读出关卡网格里每一格的星条位置与金星数量。

    星条底色是固定的，用它把格子找出来，就不受列表滚动位置影响；金星只在星条内部数，
    避开同色的罐内浅黄糖。截图是 BGR 还是 RGB 都一样，比较时只用到通道的最大/中间/最小值。

    Args:
        shot：当前截图

    Returns:
        list：[(格子中心 x, 星条顶边 y, 金星数), ...]，1280 基准
    """
    scale = shot.shape[1] / 1280
    pane = shot[:, : round(BAR_PANE_RIGHT * scale)].astype(np.int16)
    top, bottom = pane.max(axis=2), pane.min(axis=2)
    middle = pane.sum(axis=2) - top - bottom
    bar = (
        (np.abs(top - BAR_COLOR[0]) <= BAR_TOLERANCE)
        & (np.abs(middle - BAR_COLOR[1]) <= BAR_TOLERANCE)
        & (np.abs(bottom - BAR_COLOR[2]) <= BAR_TOLERANCE)
    )
    gold = (top >= 235) & (middle - bottom >= 60) & (bottom <= 160)
    cells: list[tuple[int, int, int]] = []
    rows = np.where(bar.sum(axis=1) >= BAR_MIN_ROW_PIXELS * scale)[0]
    if rows.size == 0:
        return cells
    for row_start, row_end in group_runs(rows, round(BAR_ROW_GAP * scale)):
        columns = np.where(bar[row_start : row_end + 1].sum(axis=0) >= BAR_MIN_COLUMN_PIXELS * scale)[0]
        if columns.size == 0:
            continue
        for left, right in group_runs(columns, round(BAR_CELL_GAP * scale)):
            width = right - left + 1
            if not BAR_WIDTH[0] * scale <= width <= BAR_WIDTH[1] * scale:
                continue
            inset = round(BAR_INSET * scale)
            x0, x1 = left + inset, right - inset
            y0, y1 = row_start + inset, row_end - inset
            slot = (x1 - x0 + 1) / 3
            stars = 0
            for index in range(3):
                slot_start = x0 + round(index * slot)
                slot_end = x0 + round((index + 1) * slot) - 1
                if gold[y0 : y1 + 1, slot_start : slot_end + 1].sum() >= BAR_STAR_PIXELS * scale * scale:
                    stars += 1
            cells.append((round((left + right) / 2 / scale), round(row_start / scale), stars))
    return cells


@AgentServer.custom_action("cotton_auto")
class CottonAuto(CustomAction):
    """糖果自由自动刷取：读数量 → 选组合 → 选最新三星关 → 速战。"""

    def run(self, context: Context, argv: CustomAction.RunArg) -> bool:
        """执行自动刷取。

        Args:
            context：MaaFW 上下文对象
            argv：自定义动作参数（本动作无需参数）

        Returns:
            bool：是否完成一次速战
        """
        try:
            tasker = Tasker(context)
            counts: dict[str, int] = {}

            for key in COUNT_MACHINES:
                tasker.click(*MACHINES[key]["point"]).wait()
                tasker.wait(1.2)
                found: list[tuple[str, int]] = []
                for point in ICON_POS:
                    name, value = read_reward(context, point)
                    if value >= 0:
                        found.append((name, value))
                    if len(found) >= 2:
                        break
                # 名称识别不出的那种，用该机器已知的产物组合反推
                known = {match_candy(n): v for n, v in found if match_candy(n)}
                unknown = [v for n, v in found if not match_candy(n)]
                pair = MACHINES[key]["candies"]
                missing = [c for c in pair if c not in known]
                if len(unknown) == 1 and len(missing) == 1:
                    known[missing[0]] = unknown[0]
                for candy, value in known.items():
                    if candy in counts and counts[candy] != value:
                        Prompter.log(f"{candy} 两次读数不一致（{counts[candy]} / {value}），取较小值")
                    counts[candy] = min(value, counts.get(candy, value))
                tasker.click(*BACK_POINT).wait()
                tasker.wait(0.8)
                if len(counts) >= len(COTTON_NAMES):
                    break

            if len(counts) < len(COTTON_NAMES):
                Prompter.log(f"棉花糖数量未读全（{counts}），第七关以后才会产出优质棉花糖，请先推关")
                return False

            Prompter.log("棉花糖数量 " + " / ".join(f"{c} {counts[c]}" for c in COTTON_NAMES))
            combo = choose_combo(counts)
            Prompter.log(f"选择组合 {combo}")

            tasker.click(*MACHINES[combo]["point"]).wait()
            tasker.wait(1.2)
            last = None
            for _ in range(SCROLL_MAX):
                tasker.swipe(*SCROLL_FROM, *SCROLL_TO).wait()
                tasker.wait(0.4)
                shot = tasker.screenshot()
                if last is not None and shot.shape == last.shape and (shot == last).all():
                    break
                last = shot

            tasker.wait(0.3)
            cells = [(x, y) for x, y, stars in read_grid(tasker.screenshot()) if stars >= 3]
            if not cells:
                Prompter.log("未找到三星关卡，跳过本次自动刷取")
                return False
            x, y = max(cells, key=lambda cell: (cell[1], cell[0]))  # 最下一行最右一格就是最新的三星关
            tasker.click(x, y - 20).wait()  # 星条上方就是罐子本体
            tasker.wait(0.8)

            tasker.run("速战", {"速战": {"custom_recognition_param": "e=速战_确认奖励"}})
            return True
        except Exception as error:
            return Prompter.error("糖果自由自动刷取", error)
