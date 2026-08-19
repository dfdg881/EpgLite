# EpgLite

独立的单文件 EPG 获取工具：抓取 XMLTV 节目单，自动匹配频道与别名，输出 epg.xml / epg.gz。
核心功能提取自 [Guovin/iptv-api](https://github.com/Guovin/iptv-api) 的 EPG 模块。

## 频道匹配流程（两轮，前一轮命中即不再进入后一轮）

1. 直接匹配：demo.txt/频道列表中的每个频道名是各自独立的目标频道。
先按原名精确匹配，再按规范化名称匹配（自动去除 高清/HD/括号/分隔符
等冗余；同一规范化名对应多个目标时，先按分辨率标记分流——显示名带
4K/8K/UHD/超清/杜比 等标记优先匹配带同标记的目标，如“CCTV16 4K”、
“CCTV-16-4K” 归“CCTV16-4K”，避免 4K 频道被并入普清频道；再取名称
最长包含于 EPG 频道名的目标，如“北京卫视高清”归“北京卫视”）。
2. 别名匹配：仍未匹配的 EPG 频道，通过 config/alias.txt 别名表解析
（精确别名 / re: 正则别名 / 规范化兜底），映射回目标频道。

匹配结果始终保留 demo.txt 中的原始频道名，不会因别名把多个频道合并成一个。
例如 alias 中配置“北京卫视4K,北京卫视”且 demo 中同时有“北京卫视”和
“北京卫视4K”时，两者会作为两个独立频道输出。

## 文件说明

epg.py                 主程序（单文件，仅需 Python 标准库，可选 opencc/pytz/certifi 增强）
config/epg.txt         EPG 订阅源列表（支持 \[WHITELIST] 区块、行内 Header=Value）
config/alias.txt       频道别名表（主名,别名1,别名2,...；re: 开头为正则别名）
config/demo.txt        频道列表模板（txt/#genre# 格式）
output/epg.xml     已生成的节目单示例（XMLTV 格式）
output/epg.gz      节目单 gzip 压缩版

## 快速开始

python3 epg.py

## 常用参数

\--channels 频道列表文件        （支持 txt/#genre#/m3u 格式，默认 config/demo.txt）
--epg-sources 订阅源文件        （默认 config/epg.txt）
--alias 别名表                 （默认 config/alias.txt）
--extra-epg-url URL            （临时附加 EPG 源，可多次指定）
--output-xml / --output-gz     输出路径（默认 output/epg/epg.xml、epg.gz）
--no-gz                        不生成压缩版
--include-unmatched            保留未匹配到需求频道的 EPG 频道
--days-back / --days-ahead     节目时间窗口（默认 1 / 14 天）
--fetch-concurrency            并发抓取数（默认 4）
--proxy http://127.0.0.1:7890  代理
--timezone Asia/Shanghai       时间戳时区
--auto-disable                 失败的源自动注释禁用
--verbose                      详细日志

## 可选增强（未安装自动降级）

pip install opencc    # 标题繁体转简体
pip install pytz      # 时区（Python<3.9 时需要）
pip install certifi   # HTTPS 证书

## GitHub Actions 自动更新

仓库内自带 .github/workflows/epg.yml，流程为：

1. Checkout（setup）→ 2. 安装 Python → 3. 运行脚本（步骤名 merge）→ 4. 提交结果

使用方法：

1. 把 epg.py、config/（alias.txt、epg.txt、demo.txt）和 .github/workflows/epg.yml
一起提交到 GitHub 仓库根目录；
2. 仓库 Actions 页面手动点 “Run workflow”，或按文件里的定时计划
（默认每天 UTC 03:00）自动运行；
3. 运行结束后 output/epg/epg.xml 与 epg.gz 会自动提交回仓库。
若脚本不叫 epg.py（如 merge.py），改 workflow 中 merge 步骤的
EPG\_SCRIPT 环境变量即可。

## 致谢

本项目提取并改编自 [Guovin/iptv-api](https://github.com/Guovin/iptv-api)，
感谢原作者 Guovin 的开源贡献：EPG 流式解析、多源去重合并、别名归一、
XMLTV 输出等核心设计均源自该项目。

