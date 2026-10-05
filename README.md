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
  3. 跨 2016-12-31 闰秒与跨星上时钟相关段边界的真实 HTTP 冒烟测试；

  最后以退出码报告结论并自行退出（0 全部通过，1 有失败阶段）。

仅启动服务：

```bash
docker compose up -d api
curl -s localhost:8080/healthz
```

## 接口

`POST /api/times/normalize`，每批 **1–200** 个事件，事件 `id` 必须唯一
（字符串或整数；`7` 与 `"7"` 视为同一个编号）。结果严格保持输入顺序。

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

### 星上时钟事件（可选 `correlations`）

探测器重启后，星上事件按"时钟分区 + 半开计数区间"组织。请求可在
`events` 之外另带 **1–32** 段 `correlations`，每段把该分区的一段
半开计数范围 `[tickStart, tickEnd)` 锚到 TAI 轴上；事件改用
`clockPartition` 与 `onboardTick` 引用对应分区：

```json
{
  "correlations": [
    {
      "clockPartition": 7,
      "tickStart": 0,
      "tickEnd": 1000000,
      "anchorTick": 0,
      "nanosecondsPerTickNumerator": 1000,
      "nanosecondsPerTickDenominator": 1,
      "utc": "2017-01-01T00:00:00Z"
    },
    {
      "clockPartition": 7,
      "tickStart": 1000000,
      "tickEnd": 2000000,
      "anchorTick": 1000000,
      "nanosecondsPerTickNumerator": 2000,
      "nanosecondsPerTickDenominator": 2,
      "gpsWeek": 1930,
      "gpsSecondsInWeek": 19
    }
  ],
  "events": [
    {"id": "ob-1", "clockPartition": 7, "onboardTick": 1000000},
    {"id": "ob-2", "clockPartition": 7, "onboardTick": 500000}
  ]
}
```

- 每段字段：`clockPartition`（非负整数分区号）、`tickStart`/`tickEnd`
  （半开区间，要求 `tickStart < tickEnd`）、`anchorTick`（区间内锚点
  计数，`tickStart ≤ anchorTick < tickEnd`）、`nanosecondsPerTick`
  的正整数分子/分母，以及**一个**原格式锚点——UTC 字符串
  （`utc`）或 GPS 三件套（`gpsWeek`、`gpsSecondsInWeek`[、
  `gpsNanoseconds`]），格式与合法性规则同普通事件。
- 映射为整数运算：

  ```
  TAI(t) = TAI(anchorTick) + (t - anchorTick) * 分子 / 分母
  ```

  除不尽（产生亚纳秒余数）即整批拒绝，绝不截断或取整。
- 每个星上计数必须**恰好落入一段**；区间为半开，故公共边界计数只属于
  后一段。同一分区的相邻段必须首尾相接（前一段 `tickEnd` 等于后一段
  `tickStart`，不得重叠、不得有缺口），并且两段对公共边界计数映射出的
  TAI 时刻必须完全相同。不同分区彼此独立，可以重复使用相同计数值。
- 星上事件可与 UTC、GPS 事件混排在同一批中，输出仍严格按输入顺序，
  每个结果仍只有 `id`、`taiNanoseconds`、`utc`、
  `utcTaiOffsetSeconds` 四项。
- 省略 `correlations` 时，请求、响应与失败语义与旧版完全一致；此时
  提交 `clockPartition`/`onboardTick` 事件会按"无覆盖"拒绝。

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

错误体不含任何归一化结果；`eventIndex` 为从 0 开始的批次位置，
`eventId` 回显调用方编号，`message` 给出可定位的原因。

相关段自身的错误使用 `INVALID_CORRELATION`，并以从 0 开始的
`correlationIndex` 定位出错段；涉及两段相邻关系（重叠、不连续、公共
边界 TAI 不一致或边界处亚纳秒）时再附 `relatedCorrelationIndex`：

```json
{
  "error": {
    "code": "INVALID_CORRELATION",
    "message": "segments 0 and 1 of clockPartition 7 disagree at their shared boundary tick 1000000: they map to TAI 1483228837000000000 vs 1483228838000000000",
    "correlationIndex": 0,
    "relatedCorrelationIndex": 1
  }
}
```

星上事件无覆盖或映射出亚纳秒结果时报 `INVALID_EVENT`，除
`eventIndex`/`eventId` 外还给出负责的 `correlationIndex`。所有段在
任何事件归一化之前完成校验，因此段错误不会夹带事件定位，反之错误
响应也绝不返回任何已算出的部分结果。

## 换算要点

- TAI 是均匀秒计数；UTC 标签在边界处重复。闰秒物理区间
  `[边界−1 s, 边界)` 映射到闰秒日的 `23:59:60[.frac]`，
  该区间内 TAI−UTC 仍为旧值。
- 公历换算使用 Hinnant 的 `days_from_civil / civil_from_days`
  整数算法，配合整数 `divmod` 拆分时分秒与纳秒。
- HTTP 层用 `json.loads(parse_int=…, parse_float=拒绝)` 解析：
  JSON 整数以标记字符串承载再转 `int`，从源头杜绝数字经过 `float`。

## 目录

```
app/timecore.py        # 闰秒表 + GPS/UTC/TAI 整数换算 + 星上计数段映射
app/server.py          # POST /api/times/normalize、/healthz、correlations 校验
tests/                 # unittest 用例（82 个）
verify/entrypoint.py   # 一次性 verify 编排
verify/smoke_http.py   # 跨闰秒与跨相关段边界 HTTP 冒烟
Dockerfile, docker-compose.yml
```
