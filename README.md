# 深空测控时间归一化服务

把一批 GPS 周秒事件与带闰秒的 UTC 事件归一到**同一条 TAI 纳秒时间轴**，
用于避免在闰秒边界错排遥测与遥控指令。

- 纯 Python 3.11 标准库实现，**所有换算只用整数运算**，无任何浮点参与；
- 闰秒表采用公开的 IANA tzdb `leap-seconds.list`，版本为加入
  2016-12-31 正闰秒的那一版（tzdata 2016g / IERS Bulletin C 52），
  共 28 行，末行偏移 37 s；
- 该表文件自带的过期声明（`#@`）为 **2017-06-28T00:00:00Z**，
  服务支持的年代为 `[1972-01-01T00:00:00Z, 2017-06-28T00:00:00Z)`；
- 输入支持 UTC 秒值 `60`（仅允许出现在闰秒日的 `23:59:60`）。

## 运行

```bash
# 宿主机端口可配置（默认 8080），一次性 verify 服务退出后整个编排退出：
HOST_PORT=9090 docker compose up \
  --abort-on-container-exit --exit-code-from verify
```

- `api`：常驻 HTTP 服务，带容器 `HEALTHCHECK`（轮询 `/healthz`）。
- `verify`：一次性容器，等待 API 健康后依次执行
  1. 单元测试（`python -m unittest discover`）；
  2. 镜像构建检查（全部源码字节编译、入口点导入、Dockerfile/compose
     静态校验；若挂载了 Docker CLI 与 socket 还会执行
     `docker build --check`）；
  3. 跨 2016-12-31 闰秒与跨星上相关段边界的真实 HTTP 冒烟测试；

  最后以退出码报告结论并自行退出（0 全部通过，1 有失败阶段）。

仅启动服务：

```bash
docker compose up -d api
curl -s localhost:8080/healthz
```

## 接口

`POST /api/times/normalize`，每批 **1–200** 个事件，事件 `id` 必须唯一
（字符串或整数；`7` 与 `"7"` 视为同一个编号）。结果严格保持输入顺序。

请求体除 `events` 外，可选携带 **1–32** 段 `correlations`（相关段），用于把
按时钟分区计数的星上事件接入同一条 TAI 纳秒时间轴；省略时请求、响应与失败
语义与既有版本完全一致。

### UTC 事件

```json
{
  "events": [
    {"id": "tm-1", "utc": "2016-12-31T23:59:60.5Z"}
  ]
}
```

- 必须以 `Z` 结尾；小数部分最多 9 位（纳秒）；
- 秒值 `60` 仅在闰秒日（如 2016-12-31）的 `23:59:60` 合法。

### GPS 事件

```json
{
  "events": [
    {"id": "tm-2", "gpsWeek": 1930, "gpsSecondsInWeek": 17,
     "gpsNanoseconds": 500000000}
  ]
}
```

- `gpsSecondsInWeek ∈ [0, 604799]`，`gpsNanoseconds ∈ [0, 999999999]`，
  周序号为非负整数（周 0 始于 1980-01-06T00:00:00Z）；
- GPS 恒定领先 TAI 19 s，闰秒只影响换算出的 UTC 标签。
- `gpsNanoseconds` 省略时按 0 处理。

### 星上事件（可选 `correlations`）

请求体可选给出 1–32 个相关段，事件以 `clockPartition` + `onboardTick` 引用
对应分区内的一个整数计数。每段定义一个半开计数区间到 TAI 纳秒轴的整数线性
映射：

- `clockPartition`：分区名（非空字符串）。不同分区计数彼此独立、可以重复。
- `tickRangeStart` / `tickRangeEnd`：**半开**计数范围 `[start, end)`，要求
  `end > start`。
- `anchorTick`：范围内锚点计数，必须满足 `start <= anchorTick < end`。
- 锚点时刻二选一（同一物理时刻格式不限）：
  - `anchorUtc`：原格式 UTC 字符串（与普通 UTC 事件同一套校验）；
  - GPS 锚点：`anchorGpsWeek` + `anchorGpsSecondsInWeek`，可选
    `anchorGpsNanoseconds`。
- `nanosecondsNumerator` / `nanosecondsDenominator`：每计数纳秒数的正整数
  分子、分母（均必须严格为正）。段内映射为
  `tai = anchorTai + (tick - anchorTick) * numerator / denominator`，
  **全部用整数运算，结果必须恰为整数纳秒**（除不尽即整批拒绝）。

```json
{
  "correlations": [
    {"clockPartition": "A", "tickRangeStart": 0, "tickRangeEnd": 100,
     "anchorTick": 0, "anchorUtc": "2017-01-01T00:00:00Z",
     "nanosecondsNumerator": 1000000, "nanosecondsDenominator": 1},
    {"clockPartition": "A", "tickRangeStart": 100, "tickRangeEnd": 200,
     "anchorTick": 100, "anchorUtc": "2017-01-01T00:00:00.1Z",
     "nanosecondsNumerator": 2000000, "nanosecondsDenominator": 2}
  ],
  "events": [
    {"id": "utc-ref", "utc": "2017-01-01T00:00:00Z"},
    {"id": "ob-1", "clockPartition": "A", "onboardTick": 0},
    {"id": "ob-2", "clockPartition": "A", "onboardTick": 150}
  ]
}
```

约束（任一不满足都**整批拒绝**，错误定位到相关段或事件编号）：

- 同一分区的段必须首尾相接、既不重叠也不留缺口；段在请求中的先后顺序不限。
- 同分区相接两段在公共边界计数上必须映射到**同一个 TAI 时刻**（含锚点与
  斜率的联合校验；若边界计数在任一段下产生亚纳秒结果也判该段非法）。
- 每个星上计数必须恰好落入对应分区的一段；无分区、无覆盖或亚纳秒结果均按
  事件错误（`INVALID_EVENT`，带 `eventIndex`/`eventId`）拒绝。
- 相关段自身的结构、锚点与拼接错误码为 `INVALID_CORRELATION`，带
  `correlationIndex`（从 0 开始）；相关段先于全部事件完成校验，错误响应同样
  不含任何归一化结果。

### 响应

```json
{
  "results": [
    {
      "id": "tm-1",
      "taiNanoseconds": "1483228836500000000",
      "utc": "2016-12-31T23:59:60.5Z",
      "utcTaiOffsetSeconds": "-36"
    }
  ]
}
```

- `taiNanoseconds`：十进制**字符串**形式的 TAI 纳秒
  （自 1970-01-01T00:00:00 TAI 起的整数纳秒，可为任何大整数）；
- `utc`：规范 UTC 表示（闰秒渲染为 `23:59:60`，小数尾随零被裁掉）；
- `utcTaiOffsetSeconds`：该时刻 UTC−TAI 偏移（闰秒进行中仍为旧值，
  例如 2016-12-31T23:59:60 为 `-36`，2017-01-01T00:00:00 起为 `-37`）。

**同一物理时刻**无论用 UTC 还是 GPS 提交，三项结果完全一致，
包括逐字节相同的十进制 TAI 字符串。

### 错误（整批失败，绝不返回部分结果）

非法日期、越界周内秒、非真实闰秒位置、超出支持年代、重复编号、
批大小越界、浮点 JSON 数字等都会让**整批请求**失败：

```json
{
  "error": {
    "code": "INVALID_EVENT",
    "message": "seconds value 60 is not a real leap second position: 2016-12-30T23:59:60 is not in the leap-second table",
    "eventIndex": 1,
    "eventId": "ev-42"
  }
}
```

相关段错误使用 `INVALID_CORRELATION` 与 `correlationIndex`：

```json
{
  "error": {
    "code": "INVALID_CORRELATION",
    "message": "correlation segments 0 and 1 of partition 'A' overlap: [0, 100) and [99, 200) share onboard ticks",
    "correlationIndex": 1
  }
}
```

错误体不含任何归一化结果；`eventIndex`/`correlationIndex` 均为从 0 开始的
批次位置，`eventId` 回显调用方编号，`message` 给出可定位的原因。

## 换算要点

- TAI 是均匀秒计数；UTC 标签在边界处重复。闰秒物理区间
  `[边界−1 s, 边界)` 映射到闰秒日的 `23:59:60[.frac]`，
  该区间内 TAI−UTC 仍为旧值。
- 公历换算使用 Hinnant 的 `days_from_civil / civil_from_days`
  整数算法，配合整数 `divmod` 拆分时分秒与纳秒。
- 星上计数相关段在锚点 TAI 纳秒之上做整系数一次映射，用 `divmod`
  同时求商与余数：余数非零即亚纳秒，整批拒绝；同分区相接段在公共
  边界计数上两侧分别求值并要求逐纳秒相等。
- HTTP 层用 `json.loads(parse_int=…, parse_float=拒绝)` 解析：
  JSON 整数以标记字符串承载再转 `int`，从源头杜绝数字经过 `float`。

## 目录

```
app/timecore.py        # 闰秒表 + GPS/UTC/TAI 整数换算 + 星上相关段
app/server.py          # POST /api/times/normalize、/healthz
tests/                 # unittest 用例（85 个）
verify/entrypoint.py   # 一次性 verify 编排
verify/smoke_http.py   # 跨闰秒 + 跨相关段边界 HTTP 冒烟
Dockerfile, docker-compose.yml
```
