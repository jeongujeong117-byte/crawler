"""
인간 행동과 유사한 브라우저 조작 모듈 (Human-like Interaction - `크롤링.md` 섹션 4)
- 현재 커서 위치에서 대상 요소까지 여러 단계(steps)에 걸친 베지어/가감속 곡선 커서 이동
- 클릭 시 정중앙이 아닌 미세한 랜덤 오프셋 적용
- 자연스러운 타이핑 딜레이 (글자당 50~150ms)
- page.mouse.wheel을 이용한 다단계 스크롤 및 1.0~2.8초 가변 랜덤 대기
"""
import asyncio
import math
import random
from typing import Dict, Any, Optional, Tuple
from playwright.async_api import Page, Locator, ElementHandle


class HumanActor:
    def __init__(self, page: Page, config: Optional[Dict[str, Any]] = None):
        self.page = page
        cfg = config or {}
        self.steps_min = int(cfg.get("mouse_move_steps_min", 12))
        self.steps_max = int(cfg.get("mouse_move_steps_max", 28))
        self.offset_px = int(cfg.get("click_offset_px", 5))
        self.typing_min_ms = int(cfg.get("typing_delay_min_ms", 50))
        self.typing_max_ms = int(cfg.get("typing_delay_max_ms", 150))
        self.wait_min_sec = float(cfg.get("action_wait_min_sec", 1.0))
        self.wait_max_sec = float(cfg.get("action_wait_max_sec", 2.8))
        self.scroll_min_px = int(cfg.get("scroll_step_min_px", 250))
        self.scroll_max_px = int(cfg.get("scroll_step_max_px", 550))

        # 현재 가상 마우스 커서 위치 추적
        self.current_x: float = random.uniform(200.0, 600.0)
        self.current_y: float = random.uniform(200.0, 500.0)

    async def random_wait(self, min_sec: Optional[float] = None, max_sec: Optional[float] = None) -> None:
        """클릭, 스크롤, 페이지 전환 후 1.0~2.8초 사이의 가변 랜덤 대기를 수행합니다."""
        lo = min_sec if min_sec is not None else self.wait_min_sec
        hi = max_sec if max_sec is not None else self.wait_max_sec
        delay = random.uniform(lo, hi)
        await asyncio.sleep(delay)

    async def short_pause(self, min_sec: float = 0.25, max_sec: float = 0.75) -> None:
        """동작 사이의 짧은 인간적 호흡 대기"""
        await asyncio.sleep(random.uniform(min_sec, max_sec))

    async def move_mouse_to(self, target_x: float, target_y: float) -> None:
        """
        현재 마우스 위치에서 목표 좌표(target_x, target_y)까지 여러 단계(steps)로 나누어
        약간의 곡선 궤적과 가감속을 섞어 부드럽게 이동합니다.
        """
        steps = random.randint(self.steps_min, self.steps_max)
        start_x, start_y = self.current_x, self.current_y

        # 베지어 곡선 제어점 (직선이 아닌 살짝 휘어지는 자연스러운 손 움직임)
        ctrl_x = (start_x + target_x) / 2.0 + random.uniform(-45.0, 45.0)
        ctrl_y = (start_y + target_y) / 2.0 + random.uniform(-35.0, 35.0)

        for i in range(1, steps + 1):
            t = i / steps
            # Ease-out 보간
            ease_t = 1 - math.pow(1 - t, 2)
            x = (1 - ease_t) ** 2 * start_x + 2 * (1 - ease_t) * ease_t * ctrl_x + (ease_t ** 2) * target_x
            y = (1 - ease_t) ** 2 * start_y + 2 * (1 - ease_t) * ease_t * ctrl_y + (ease_t ** 2) * target_y
            await self.page.mouse.move(x, y)
            await asyncio.sleep(random.uniform(0.006, 0.018))

        self.current_x, self.current_y = target_x, target_y

    async def human_click(self, target: Locator | ElementHandle, wait_after: bool = True) -> bool:
        """
        대상 요소의 영역(Bounding Box)을 계산한 뒤, 정중앙이 아닌 랜덤 오프셋 좌표로
        마우스를 부드럽게 이동시켜 클릭합니다.
        """
        try:
            if isinstance(target, Locator):
                await target.scroll_into_view_if_needed(timeout=4000)
                box = await target.bounding_box(timeout=4000)
            else:
                await target.scroll_into_view_if_needed()
                box = await target.bounding_box()

            if not box:
                return False

            # 요소 크기에 맞춰 안전한 랜덤 오프셋 계산
            max_ox = min(self.offset_px, max(1.0, box["width"] * 0.25))
            max_oy = min(self.offset_px, max(1.0, box["height"] * 0.25))
            offset_x = random.uniform(-max_ox, max_ox)
            offset_y = random.uniform(-max_oy, max_oy)

            target_x = box["x"] + (box["width"] / 2.0) + offset_x
            target_y = box["y"] + (box["height"] / 2.0) + offset_y

            await self.move_mouse_to(target_x, target_y)
            await self.short_pause(0.08, 0.22)
            await self.page.mouse.down()
            await asyncio.sleep(random.uniform(0.04, 0.11))
            await self.page.mouse.up()

            if wait_after:
                await self.random_wait()
            return True
        except Exception:
            # 폴백: 일반 클릭 시도
            try:
                await target.click(timeout=4000)
                if wait_after:
                    await self.random_wait()
                return True
            except Exception:
                return False

    async def human_type(self, locator: Locator, text: str, clear_first: bool = True) -> None:
        """
        입력창을 사람처럼 클릭한 뒤, 글자당 50~150ms 랜덤 딜레이를 주며 자연스럽게 타이핑합니다.
        """
        await self.human_click(locator, wait_after=False)
        await self.short_pause(0.2, 0.5)
        if clear_first:
            await locator.fill("")
            await self.short_pause(0.1, 0.3)

        for ch in text:
            delay_ms = random.randint(self.typing_min_ms, self.typing_max_ms)
            await self.page.keyboard.type(ch, delay=delay_ms)

        await self.short_pause(0.3, 0.7)

    async def human_scroll_down(self, chunks: int = 2, wait_after: bool = True) -> None:
        """
        한 번에 끝까지 점프하지 않고, page.mouse.wheel을 사용해 여러 번 나누어 스크롤한 뒤
        사람이 글을 읽는 것처럼 가변 대기(1.0~2.8초)합니다.
        """
        for _ in range(chunks):
            delta_y = random.randint(self.scroll_min_px, self.scroll_max_px)
            # 미세한 커서 흔들림 동반
            jitter_x = self.current_x + random.uniform(-12.0, 12.0)
            jitter_y = self.current_y + random.uniform(-12.0, 12.0)
            await self.page.mouse.move(max(50.0, jitter_x), max(50.0, jitter_y), steps=4)
            await self.page.mouse.wheel(0, delta_y)
            await asyncio.sleep(random.uniform(0.25, 0.65))

        if wait_after:
            await self.random_wait()

    async def human_scroll_to_read(self) -> None:
        """상세 페이지 진입 후 본문과 댓글을 읽듯 자연스럽게 1~3회 스크롤합니다."""
        scroll_times = random.randint(1, 3)
        await self.human_scroll_down(chunks=scroll_times, wait_after=True)
