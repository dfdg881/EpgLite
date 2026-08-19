# EpgLite
[English](./README_en.md) | 中文

EPG 整合工具：抓取 XMLTV 节目单，自动匹配设定的频道与别名，输出文件

#使用
https://raw.githubusercontent.com/dfdg881/EpgLite/refs/heads/main/output/epg.gz
https://cdn.jsdelivr.net/gh/dfdg881/EpgLite@main/output/epg.gz

## 本地运行
``` python
pip install opencc-python-reimplemented certifi pytz
python epg.py --[运行参数]
```


| 参数 | 说明 | 默认值 |
|------|------|--------|
| `--channels` | 频道列表文件（支持 txt/#genre#/m3u 格式） | `config/demo.txt` |
| `--epg-sources` | 订阅源文件 | `config/epg.txt` |
| `--alias` | 别名表文件 | `config/alias.txt` |
| `--extra-epg-url` | 临时附加 EPG 源 URL（可多次指定） | 无 |
| `--output-xml` | 输出 XML 文件路径 | `output/epg/epg.xml` |
| `--output-gz` | 输出 GZ 压缩文件路径 | `output/epg/epg.gz` |
| `--no-gz` | 不生成 .gz 压缩文件 | 生成 |
| `--include-unmatched` | 保留未匹配到需求频道的 EPG 频道 | 不保留 |
| `--days-back` | 节目回顾天数 | `1` 天 |
| `--days-ahead` | 节目预告天数 | `14` 天 |
| `--fetch-concurrency` | 并发抓取数 | `4` |
| `--proxy` | HTTP 代理地址 | 无 |
| `--timezone` | 时间戳时区 | `Asia/Shanghai` |
| `--auto-disable` | 失败的源自动注释禁用 | 不自动禁用 |
| `--verbose` | 详细日志输出 | 不启用 |

> **说明：** `--output-xml` 和 `--output-gz` 可以同时使用，分别指定两种格式的输出路径。`--extra-epg-url` 支持多次指定以附加多个 EPG 源。

## 致谢

[Guovin/iptv-api](https://github.com/Guovin/iptv-api)

[mytv-android/myEPG](https://github.com/mytv-android/myEPG)

代码为DeepSeek Harness生成，如有问题请联系https://www.deepseek.com/harness/

