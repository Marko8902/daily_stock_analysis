# Baostock 数据源接入指南（可复用）

> 本文从一个生产级 A 股分析项目中提炼，完整描述 **Baostock（证券宝）** 行情数据源的接入方式、关键设计与避坑点。
> 代码以「最小可复用」为目标整理，可直接拷贝到其他 Python 项目中使用。

---

## 1. Baostock 是什么 / 适用场景

[Baostock（证券宝）](http://baostock.com) 是一个**免费、无需注册、无需 Token、无配额限制**的证券历史数据接口。

| 特点 | 说明 |
|------|------|
| 免费 | 完全免费，无需 API Key |
| 无配额 | 没有频率/次数限制（适合做兜底数据源） |
| 需登录 | 每次使用前需 `bs.login()`，用完 `bs.logout()`（**匿名登录**，不需要账号密码） |
| 数据范围 | A 股（沪深）日线/周线/月线、分钟线、复权因子、基本面等 |
| 时效 | T+1（盘后更新），**不适合实时行情** |
| 不支持 | ❌ 港股、❌ 美股、❌ 北交所 |

**典型定位**：作为多数据源容错链里的**稳定兜底源**。东方财富/新浪等实时源被限流或网络异常时，Baostock 因无配额、接口稳定，常能成功返回历史日线。

---

## 2. 安装

```bash
pip install baostock>=0.8.0
```

> 注意：Baostock 依赖 `pandas`。部分老版本对新版 `pandas`/`numpy` 兼容性一般，生产中建议锁定一个验证过的组合（本项目使用 `baostock 0.8.x+`）。

---

## 3. 核心设计：连接生命周期管理（最重要）

Baostock 与多数 HTTP 接口不同——它是**有状态会话**：必须先 `login()` 再查询，用完 `logout()`。如果不登出，会**泄露连接**。

最佳实践是用**上下文管理器**封装 `login/logout`，保证异常时也能登出：

```python
import logging
from contextlib import contextmanager
from typing import Generator

logger = logging.getLogger(__name__)


class DataFetchError(Exception):
	"""数据获取异常"""


@contextmanager
def baostock_session() -> Generator:
	"""
	Baostock 连接上下文管理器

	- 进入时自动 login()
	- 退出时自动 logout()（即使发生异常也会登出，防止连接泄露）

	用法:
		with baostock_session() as bs:
			rs = bs.query_history_k_data_plus(...)
	"""
	import baostock as bs

	login_result = bs.login()
	if login_result.error_code != '0':
		raise DataFetchError(f"Baostock 登录失败: {login_result.error_msg}")
	logger.debug("Baostock 登录成功")

	try:
		yield bs
	finally:
		try:
			logout_result = bs.logout()
			if logout_result.error_code != '0':
				logger.warning(f"Baostock 登出异常: {logout_result.error_msg}")
		except Exception as e:
			logger.warning(f"Baostock 登出时发生错误: {e}")
```

> ⚠️ **关键点**：`logout()` 放在 `finally` 里。即使查询抛异常，也必须登出，否则长时间运行的程序会累积泄露的 Baostock 连接。

---

## 4. 股票代码格式转换

Baostock 要求的代码格式是 **`{交易所}.{6位代码}`**，例如：

- 沪市：`sh.600519`
- 深市：`sz.000001`

而业务侧通常传入裸 6 位代码（`600519`）。转换规则如下：

```python
def convert_stock_code(stock_code: str) -> str:
	"""
	将裸代码转换为 Baostock 格式：600519 -> sh.600519, 000001 -> sz.000001

	判断规则（按前缀）：
	  沪市 sh: 600/601/603/605/688 开头；ETF 51/52/56/58
	  深市 sz: 000/001/002/003/300/301 开头；ETF 15/16/18
	"""
	code = stock_code.strip().lower()

	# 已是 baostock 格式，直接返回
	if code.startswith(('sh.', 'sz.')):
		return code

	code = code.upper().replace('SH', '').replace('SZ', '').replace('.', '')
	code = ''.join(ch for ch in code if ch.isdigit())  # 仅保留数字

	if len(code) != 6:
		raise DataFetchError(f"无法识别的股票代码: {stock_code}")

	# ETF
	if code.startswith(('51', '52', '56', '58')):
		return f"sh.{code}"
	if code.startswith(('15', '16', '18')):
		return f"sz.{code}"

	# 个股
	if code.startswith(('600', '601', '603', '605', '688')):
		return f"sh.{code}"
	if code.startswith(('000', '001', '002', '003', '300', '301')):
		return f"sz.{code}"

	logger.warning(f"无法确定股票 {code} 的市场，默认使用深市")
	return f"sz.{code}"
```

> 提示：港股/美股/北交所不被 Baostock 支持，建议在转换前先判断并抛异常，让上层切换到其他数据源（见第 7 节）。

---

## 5. 获取日线数据

核心接口是 `query_history_k_data_plus()`。Baostock 返回的是**逐行游标**，需用 `while rs.next()` 迭代，且**所有字段都是字符串**，必须自行转数值类型。

```python
import pandas as pd


def fetch_daily(stock_code: str, start_date: str, end_date: str) -> pd.DataFrame:
	"""
	获取日线数据并标准化。

	Args:
		stock_code: 裸代码，如 '600519'
		start_date: 'YYYY-MM-DD'
		end_date:   'YYYY-MM-DD'

	Returns:
		标准列 DataFrame: [date, open, high, low, close, volume, amount, pct_chg]
	"""
	bs_code = convert_stock_code(stock_code)

	with baostock_session() as bs:
		rs = bs.query_history_k_data_plus(
			code=bs_code,
			fields="date,open,high,low,close,volume,amount,pctChg",
			start_date=start_date,
			end_date=end_date,
			frequency="d",      # d=日线, w=周线, m=月线, 5/15/30/60=分钟线
			adjustflag="2",     # 1=后复权, 2=前复权, 3=不复权
		)

		if rs.error_code != '0':
			raise DataFetchError(f"Baostock 查询失败: {rs.error_msg}")

		# Baostock 返回逐行游标，需迭代取出
		rows = []
		while rs.next():
			rows.append(rs.get_row_data())

		if not rows:
			raise DataFetchError(f"Baostock 未查询到 {stock_code} 的数据")

		df = pd.DataFrame(rows, columns=rs.fields)

	# --- 标准化 ---
	df = df.rename(columns={'pctChg': 'pct_chg'})

	numeric_cols = ['open', 'high', 'low', 'close', 'volume', 'amount', 'pct_chg']
	for col in numeric_cols:
		df[col] = pd.to_numeric(df[col], errors='coerce')  # 字符串 -> 数值

	df['date'] = pd.to_datetime(df['date'])
	df = df.dropna(subset=['close']).sort_values('date').reset_index(drop=True)

	return df[['date'] + numeric_cols]
```

### 复权参数 `adjustflag`

| 值 | 含义 | 说明 |
|----|------|------|
| `"1"` | 后复权 | 以上市首日为基准 |
| `"2"` | 前复权 | 以最新价为基准（**做技术分析常用**） |
| `"3"` | 不复权 | 原始成交价 |

### 常用 `fields` 字段

`date, open, high, low, close, preclose, volume, amount, adjustflag, turn, tradestatus, pctChg, isST`

---

## 6. 重试策略（指数退避）

Baostock 偶发网络抖动，建议加重试。用 `tenacity` 对**连接类异常**做指数退避：

```python
from tenacity import (
	retry, stop_after_attempt, wait_exponential,
	retry_if_exception_type, before_sleep_log,
)

@retry(
	stop=stop_after_attempt(3),
	wait=wait_exponential(multiplier=1, min=2, max=30),  # 2s, 4s, 8s... 上限30s
	retry=retry_if_exception_type((ConnectionError, TimeoutError)),
	before_sleep=before_sleep_log(logger, logging.WARNING),
)
def fetch_daily_with_retry(stock_code, start_date, end_date):
	return fetch_daily(stock_code, start_date, end_date)
```

> 只对 `ConnectionError/TimeoutError` 重试；对「无数据」「代码非法」这类业务错误不重试（重试也没用）。

---

## 7. 不支持的市场要主动拦截

Baostock **只支持 A 股沪深**。在请求前先判断并抛异常，便于上层多源容错链切换到其他数据源：

```python
import re

def assert_supported(stock_code: str):
	code = stock_code.strip().upper()
	# 美股: 1-5 个字母
	if re.match(r'^[A-Z]{1,5}(\.[A-Z])?$', code):
		raise DataFetchError(f"Baostock 不支持美股 {code}")
	# 港股: 5 位数字 或 .HK 后缀
	if code.endswith('.HK') or (code.isdigit() and len(code) == 5):
		raise DataFetchError(f"Baostock 不支持港股 {code}")
	# 北交所: 8x 开头 / 4x 开头（部分）
	if code.startswith(('43', '83', '87', '88')):
		raise DataFetchError(f"Baostock 不支持北交所 {code}")
```

---

## 8. 其他常用查询

```python
# 股票名称 / 基本信息
with baostock_session() as bs:
	rs = bs.query_stock_basic(code="sh.600519")
	while rs.next():
		row = rs.get_row_data()   # [code, code_name, ipoDate, outDate, type, status]

# 全部股票列表
with baostock_session() as bs:
	rs = bs.query_stock_basic()
	rows = []
	while rs.next():
		rows.append(rs.get_row_data())
	df = pd.DataFrame(rows, columns=rs.fields)
```

> 下面几节的接口都返回同样的「逐行游标」，先封装一个通用工具函数减少重复：

```python
def rs_to_df(rs) -> pd.DataFrame:
	"""把 Baostock 游标结果转成 DataFrame（所有 query_* 接口通用）"""
	if rs.error_code != '0':
		raise DataFetchError(f"Baostock 查询失败: {rs.error_msg}")
	rows = []
	while rs.next():
		rows.append(rs.get_row_data())
	return pd.DataFrame(rows, columns=rs.fields)
```

---

## 9. 周线 / 月线 / 分钟线（其他周期）

仍是 `query_history_k_data_plus()`，只改 `frequency`。注意两点：
- **周线/月线**：字段和日线类似，但**没有**分钟级 `time` 列。
- **分钟线**（`5/15/30/60`）：字段里多一个 `time`（格式 `20240102093500000`），且**没有** `turn / pctChg / isST` 等字段；数据量大，区间别拉太长。

```python
def fetch_kline(stock_code, start_date, end_date, frequency="w", adjustflag="2"):
	"""按指定周期获取 K 线。frequency: d/w/m 或 5/15/30/60。"""
	bs_code = convert_stock_code(stock_code)

	# 分钟线必须带 time 字段；日/周/月线没有 time
	if frequency in ("5", "15", "30", "60"):
		fields = "date,time,code,open,high,low,close,volume,amount,adjustflag"
	else:
		fields = "date,code,open,high,low,close,volume,amount,adjustflag,turn,pctChg"

	with baostock_session() as bs:
		rs = bs.query_history_k_data_plus(
			code=bs_code, fields=fields,
			start_date=start_date, end_date=end_date,
			frequency=frequency, adjustflag=adjustflag,
		)
		df = rs_to_df(rs)

	# 数值化（time 列保留为字符串标记，不转）
	for c in ['open', 'high', 'low', 'close', 'volume', 'amount', 'turn', 'pctChg']:
		if c in df.columns:
			df[c] = pd.to_numeric(df[c], errors='coerce')
	return df


df_week  = fetch_kline("600519", "2023-01-01", "2024-01-01", frequency="w")   # 周线
df_month = fetch_kline("600519", "2020-01-01", "2024-01-01", frequency="m")   # 月线
df_30m   = fetch_kline("600519", "2024-01-01", "2024-01-31", frequency="30")  # 30 分钟线
```

| frequency | 含义 | 备注 |
|----|----|----|
| `d` | 日线 | 默认 |
| `w` | 周线 | 一周一根 |
| `m` | 月线 | 一月一根 |
| `5/15/30/60` | 分钟线 | 多一个 `time` 字段，无 `turn/pctChg`；区间别太长 |

> 周线/月线同样支持 `adjustflag` 复权；分钟线只能用 `5/15/30/60` 这几个值。

---

## 10. 复权因子 & 指数成分股 & 行业分类

```python
# 复权因子：想自己做复权计算时用
with baostock_session() as bs:
	rs = bs.query_adjust_factor(code="sh.600519",
								start_date="2023-01-01", end_date="2024-01-01")
	df_adj = rs_to_df(rs)
	# 字段: code, dividOperateDate, foreAdjustFactor, backAdjustFactor, adjustFactor

# 指数成分股：沪深300 / 上证50 / 中证500
with baostock_session() as bs:
	hs300 = rs_to_df(bs.query_hs300_stocks())   # 字段: updateDate, code, code_name
	sz50  = rs_to_df(bs.query_sz50_stocks())
	zz500 = rs_to_df(bs.query_zz500_stocks())

# 行业分类（申万一级）
with baostock_session() as bs:
	rs = bs.query_stock_industry(code="sh.600519")
	df_ind = rs_to_df(rs)   # 字段: updateDate, code, code_name, industry, industryClassification
```

---

## 11. 基本面数据（季频财务）

Baostock 的财务接口大多按「年 + 季度」查询，签名统一为 `query_xxx_data(code, year, quarter)`，`quarter` 取 1-4：

```python
def fetch_fundamental(stock_code, year, quarter):
	"""一次性拉取某季度的盈利/营运/成长/偿债/现金流/杜邦指标。"""
	bs_code = convert_stock_code(stock_code)
	out = {}
	with baostock_session() as bs:
		out['profit']    = rs_to_df(bs.query_profit_data(bs_code, year=year, quarter=quarter))     # 盈利能力
		out['operation'] = rs_to_df(bs.query_operation_data(bs_code, year=year, quarter=quarter))  # 营运能力
		out['growth']    = rs_to_df(bs.query_growth_data(bs_code, year=year, quarter=quarter))      # 成长能力
		out['balance']   = rs_to_df(bs.query_balance_data(bs_code, year=year, quarter=quarter))     # 偿债能力
		out['cash_flow'] = rs_to_df(bs.query_cash_flow_data(bs_code, year=year, quarter=quarter))   # 现金流量
		out['dupont']    = rs_to_df(bs.query_dupont_data(bs_code, year=year, quarter=quarter))      # 杜邦分析
	return out


f = fetch_fundamental("600519", year=2023, quarter=3)
print(f['profit'][['code', 'roeAvg', 'npMargin', 'epsTTM']])
```

常用财务接口一览：

| 接口 | 内容 | 关键字段 |
|----|----|----|
| `query_profit_data` | 盈利能力 | `roeAvg, npMargin, gpMargin, epsTTM, netProfit` |
| `query_operation_data` | 营运能力 | `NRTurnRatio, INVTurnRatio, AssetTurnRatio` |
| `query_growth_data` | 成长能力 | `YOYEquity, YOYAsset, YOYNI` |
| `query_balance_data` | 偿债能力 | `currentRatio, quickRatio, liabilityToAsset` |
| `query_cash_flow_data` | 现金流量 | `CAToAsset, CFOToNP, CFOToGr` |
| `query_dupont_data` | 杜邦分析 | `dupontROE, dupontAssetStoEquity, dupontNitogr` |

分红 / 业绩快报 / 业绩预告（按年份或公告日期区间查询）：

```python
with baostock_session() as bs:
	# 分红：yearType='report'(预案公告年) 或 'operate'(除权除息年)
	div = rs_to_df(bs.query_dividend_data(code="sh.600519", year="2023", yearType="report"))
	# 业绩快报：按公告日期区间
	express = rs_to_df(bs.query_performance_express_report(
		code="sh.600519", start_date="2023-01-01", end_date="2024-01-01"))
	# 业绩预告
	forecast = rs_to_df(bs.query_forecast_report(
		code="sh.600519", start_date="2023-01-01", end_date="2024-01-01"))
```

> 季频财务数据在对应季报披露后才更新；并非每只股票每个季度都有全部字段，取数后注意判空（`pd.to_numeric(..., errors='coerce')`）。

---

## 12. 融入「多数据源容错链」(可选)

如果你的项目有多个数据源，推荐用**抽象基类 + 模板方法**，让 Baostock 只实现「取原始数据」和「标准化」两步，公共流程（日期计算、清洗、指标计算、日志）由基类统一处理：

```python
from abc import ABC, abstractmethod

STANDARD_COLUMNS = ['date', 'open', 'high', 'low', 'close', 'volume', 'amount', 'pct_chg']

class BaseFetcher(ABC):
	name: str = "BaseFetcher"
	priority: int = 99            # 数字越小越优先

	@abstractmethod
	def _fetch_raw_data(self, code, start, end) -> pd.DataFrame: ...

	@abstractmethod
	def _normalize_data(self, df, code) -> pd.DataFrame: ...

	def get_daily_data(self, code, start=None, end=None, days=30) -> pd.DataFrame:
		# 1.算日期 2.子类取数 3.子类标准化 4.清洗 5.算指标
		raw = self._fetch_raw_data(code, start, end)
		return self._normalize_data(raw, code)


class BaostockFetcher(BaseFetcher):
	name = "BaostockFetcher"
	priority = 3                  # 作为兜底，优先级排后

	def _fetch_raw_data(self, code, start, end):
		assert_supported(code)
		# ... 第 5 节的 query_history_k_data_plus 逻辑，返回原始 df ...

	def _normalize_data(self, df, code):
		# ... 重命名 pctChg->pct_chg、转数值、保留标准列 ...
		return df
```

容错链调度（简化版）：把所有数据源按 `priority` 排序，逐个尝试，失败自动切下一个：

```python
fetchers = sorted([EfinanceFetcher(), AkshareFetcher(), BaostockFetcher()],
				  key=lambda f: f.priority)

def get_daily(code, **kw):
	last_err = None
	for f in fetchers:
		try:
			return f.get_daily_data(code, **kw)
		except DataFetchError as e:
			last_err = e
			logger.warning(f"[{f.name}] {code} 失败，切换下一个: {e}")
	raise last_err
```

> 经验：把 Baostock 的 `priority` 设大一点（排在实时源之后）。实时源平时更快、含量比/换手率等字段；Baostock 在它们被限流/网络异常时兜底。

---

## 13. 避坑清单（Lessons Learned）

| 坑 | 说明 / 对策 |
|----|------|
| **忘记 logout** | 必须用 `try/finally` 或上下文管理器登出，否则连接泄露。 |
| **返回值是字符串** | 所有数值字段都是 `str`，务必 `pd.to_numeric(..., errors='coerce')`。 |
| **游标必须迭代** | 结果不是 DataFrame，要 `while rs.next(): rs.get_row_data()`。 |
| **检查 `error_code`** | `login/query/logout` 都返回对象，`error_code != '0'` 即失败，别只看异常。 |
| **代码要带前缀** | 必须 `sh.`/`sz.`，传裸 `600519` 查不到。 |
| **不支持港美股/北交所** | 请求前主动拦截并抛异常，交给其他数据源。 |
| **T+1 数据** | 盘后更新，不能用于盘中实时行情。 |
| **多线程不友好** | Baostock 会话是进程级状态，**不要在多线程里并发** login/query；如需并发，给它单独串行或加锁。 |
| **SSL 拦截环境** | 若公司网络对 HTTPS 做 SSL 拦截导致证书报错，Python 端可安装 `pip-system-certs` 改用系统证书库。 |

---

## 14. 一分钟最小示例

```python
import baostock as bs
import pandas as pd

bs.login()
rs = bs.query_history_k_data_plus(
	"sh.600519",
	"date,open,high,low,close,volume,amount,pctChg",
	start_date="2024-01-01", end_date="2024-03-01",
	frequency="d", adjustflag="2",
)
rows = []
while rs.next():
	rows.append(rs.get_row_data())
bs.logout()

df = pd.DataFrame(rows, columns=rs.fields)
for c in ["open", "high", "low", "close", "volume", "amount", "pctChg"]:
	df[c] = pd.to_numeric(df[c], errors="coerce")
print(df.tail())
```

---

### 参考

- 官网 / 文档：http://baostock.com
- 复权说明、字段含义、分钟线频率等以官网为准。
