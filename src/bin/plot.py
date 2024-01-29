#!/usr/bin/env python3
"""
Installation:
```shell
pip install "drawsvg>=2,<3" "pydantic>=2,<3"
```

Usage:
```shell
python plot.py traces.ndjson
```
"""
import os
from argparse import ArgumentParser
from collections import defaultdict
from pathlib import Path

from drawsvg import Drawing, Text, Rectangle
from pydantic import BaseModel

padding_top = 5
padding_bottom = 5
padding_left = 5
padding_right = 5
text_col_width = 250
content_col_width = 850
bar_height = 20
multi_lane_padding = 1
section_padding_height = 10


class Instant(BaseModel):
    secs: int
    nanos: int


class Span(BaseModel):
    id: int
    name: str
    start: Instant
    end: Instant
    parents: list[int]
    is_main_thread: bool
    fields: dict[str, str]

    def start_secs(self) -> float:
        return self.start.secs + self.start.nanos / 10**9

    def end_secs(self) -> float:
        return self.end.secs + self.end.nanos / 10**9

    def duration(self) -> float:
        return self.end_secs() - self.start_secs()


class FullSpan(Span):
    """The full span from start to end, including the holes where the span
    wasn't active."""

    pass


def main():
    parser = ArgumentParser()
    parser.add_argument(
        "input",
        default=os.environ.get("TRACING_DURATION_FILE"),
        help="The ndjson file generated by the rust process",
    )
    parser.add_argument(
        "--output",
        help="The name of the svg file to be written."
        "Defaults to the input filename with `.svg` as extension",
    )
    parser.add_argument(
        "--multi-lane",
        action="store_true",
        help="Don't overlay spans",
    )
    parser.add_argument(
        "--min-length",
        type=float,
        default=None,
        help="Filter out spans shorter spans (unit: seconds)",
    )
    parser.add_argument(
        "--remove",
        nargs="*",
        help="Remove this span name (multiple use)",
    )
    parser.add_argument(
        "--inline-field",
        action="store_true",
        help="If the is only one field, display its value inline. "
        "Since the text is not limited to its box, text can overlap and "
        "become unreadable.",
    )
    # See http://www.cookbook-r.com/Graphs/Colors_(ggplot2)/#a-colorblind-friendly-palette
    parser.add_argument(
        "--color-top-blocking",
        default="#E69F0088",
        help="The color for the upper section of span active time when running "
        "on the main thread",
    )
    parser.add_argument(
        "--color-top-threadpool",
        default="#009E7388",
        help="The color for the upper section of span active time when running "
        "off the main thread (with `tokio::task::spawn_blocking`)",
    )
    parser.add_argument(
        "--color-bottom",
        default="#56B4E988",
        help="The color for the lower section of span total time",
    )
    args = parser.parse_args()

    spans = []
    for line in Path(args.input).read_text().splitlines():
        if not line:
            continue
        if Span.model_validate_json(line).name in (args.remove or []):
            continue
        spans.append(Span.model_validate_json(line))

    # noinspection PyTypeChecker
    last_end = max(spans, key=Span.end_secs).end_secs()

    full_spans: dict[int, FullSpan] = {}
    for span in spans:
        if full_span := full_spans.get(span.id):
            assert span.end_secs() > full_span.end_secs()
            full_spans[span.id].end = span.end
        else:
            full_spans[span.id] = FullSpan(**span.__dict__)

    # Remove to short spans
    removed_span_ids = set()
    if args.min_length:
        for span_id, full_span in full_spans.items():
            if full_span.duration() < args.min_length:
                removed_span_ids.add(span_id)
        for removed_span_id in removed_span_ids:
            del full_spans[removed_span_id]
        spans = [span for span in spans if span.id not in removed_span_ids]

    # In expanded mode, we avoid overlaps in different lanes, so we track
    # until which timestamp each lane is blocked and how many lanes we need.
    lanes_end: dict[str, list[float]] = defaultdict(list)
    span_lanes: dict[int, int] = {}
    for full_span in full_spans.values():
        if args.multi_lane:
            for idx in range(len(lanes_end[full_span.name])):
                if lanes_end[full_span.name][idx] < full_span.start_secs():
                    lanes_end[full_span.name][idx] = full_span.end_secs()
                    span_lanes[full_span.id] = idx
                    break
            else:
                span_lanes[full_span.id] = len(lanes_end[full_span.name])
                lanes_end[full_span.name].append(full_span.end_secs())
        else:
            span_lanes[full_span.id] = 0
            lanes_end[full_span.name] = [full_span.end_secs()]
    lanes_end = dict(lanes_end)

    extra_lane_height = bar_height // 2 + multi_lane_padding

    # For the left sidebar, sort spans by the first time a span name occurred
    earliest_starts = dict()
    for span in spans:
        if current_earliest := earliest_starts.get(span.name):
            if span.start_secs() < current_earliest:
                earliest_starts[span.name] = span.start_secs()
        else:
            earliest_starts[span.name] = span.start_secs()
    earliest_starts = sorted(earliest_starts.items(), key=lambda x: x[1])
    # Top row is for the timeline
    name_offsets = {
        name: index + 1 for index, (name, _start) in enumerate(earliest_starts)
    }
    extra_lanes_cur = 0
    extra_lanes_cumulative = {}
    for name, _start in earliest_starts:
        extra_lanes_cumulative[name] = extra_lanes_cur
        extra_lanes_cur += len(lanes_end[name]) - 1

    # Don't forget the timeline row
    total_height = (
        padding_top
        + (bar_height + section_padding_height) * (len(name_offsets) + 1)
        + extra_lane_height * extra_lanes_cur
        + padding_bottom
    )

    d = Drawing(
        padding_left + text_col_width + content_col_width + padding_right,
        total_height,
        origin="top-left",
    )

    if args.min_length:
        # Add a note about filtered out spans
        d.append(
            Text(
                f"only spans >{args.min_length}s",
                "1em",
                x=padding_left,
                y=padding_top + bar_height // 2,
                dominant_baseline="middle",
                text_anchor="start",
            )
        )

    # Draw the "timeline"
    d.append(
        Text(
            f"{0:.3f}s",
            "1em",
            x=text_col_width,
            y=padding_top + bar_height // 2,
            dominant_baseline="middle",
            text_anchor="start",
        )
    )
    d.append(
        Text(
            f"{last_end:.3f}s",
            "1em",
            x=text_col_width + content_col_width,
            y=padding_top + bar_height // 2,
            dominant_baseline="middle",
            text_anchor="end",
        )
    )

    # Draw the legend on the left
    for name, offset in name_offsets.items():
        y = (
            padding_top
            + bar_height // 2
            + offset * (bar_height + section_padding_height)
            + extra_lane_height * extra_lanes_cumulative[name]
        )
        d.append(
            Text(
                name,
                "1em",
                x=padding_left,
                y=y,
                dominant_baseline="middle",
            )
        )

    # Draw the active top half of each span
    for span in spans:
        offset = name_offsets[span.name]

        # Show name, duration and fields
        tooltip = (
            span.name
            + f" {span.duration():.3f}s\n"
            + "\n".join(f"{key}: {value}" for key, value in span.fields.items())
        )

        x = text_col_width + content_col_width * span.start_secs() / last_end
        y = (
            offset * (bar_height + section_padding_height)
            + extra_lane_height * extra_lanes_cumulative[span.name]
        )
        width = content_col_width * span.duration() / last_end
        height = bar_height // 2
        color = (
            args.color_top_blocking
            if span.is_main_thread
            else args.color_top_threadpool
        )
        r = Rectangle(x, y, width, height, fill=color)
        r.append_title(tooltip)
        d.append(r)

    # Draw the total bottom half of each span
    for full_span in full_spans.values():
        offset = name_offsets[full_span.name]
        # Show name, duration and fields
        tooltip = (
            full_span.name
            + f" {full_span.end_secs() - full_span.start_secs():.3f}s\n"
            + "\n".join(f"{key}: {value}" for key, value in full_span.fields.items())
        )
        x = text_col_width + content_col_width * full_span.start_secs() / last_end
        # lower half
        y = (
            offset * (bar_height + section_padding_height)
            + extra_lane_height * extra_lanes_cumulative[full_span.name]
            + extra_lane_height * span_lanes[full_span.id]
            + bar_height // 2
        )
        width = (
            content_col_width
            * (full_span.end_secs() - full_span.start_secs())
            / last_end
        )
        height = bar_height // 2
        r = Rectangle(x, y, width, height, fill=args.color_bottom)
        r.append_title(tooltip)
        d.append(r)
        if args.inline_field and len(full_span.fields) == 1:
            text = next(iter(full_span.fields.values()))
            d.append(
                Text(
                    text,
                    "0.7em",
                    x=x,
                    y=y + height // 2,
                    dominant_baseline="middle",
                    text_anchor="start",
                )
            )

    d.save_svg(args.output or Path(args.input).with_suffix(".svg"))


if __name__ == "__main__":
    main()
