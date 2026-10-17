# 景区人群计数代码测试视频（5段）

本目录实际下载并整理了5个不同来源的视频，其中1段为安徽黄山，另4段为北京长城、故宫、天坛及上海传统旅游街区。已检查预览帧，五段均有人群画面；黄山新闻片也包含纯风景和采访。

`videos/` 是可直接使用的 H.264 MP4，`originals/` 保留下载原件。测试副本统一移除音轨、最大1080p，不放大原始360p画面，也未变速。

| 视频 | 地点 | 时长 | 分辨率 | 测试侧重 |
|---|---|---:|---|---|
| [01_安徽黄山_雪景与游客](videos/01_安徽黄山_雪景与游客.mp4) | 安徽黄山风景区 | 132.2秒 | 640×360 | 多镜头新闻片；台阶远景、遮挡、采访近景；建议先取15–20秒或末尾游客步道片段。 |
| [02_北京长城_游客通道](videos/02_北京长城_游客通道.mp4) | 北京长城（来源页面标注） | 16.75秒 | 1920×1080 | 长城游客通道；检查远近尺度和遮挡。 |
| [03_北京故宫_院落游客](videos/03_北京故宫_院落游客.mp4) | 北京故宫（来源页面标注） | 18.08秒 | 1920×1012 | 院落游客；检查较密人群、小目标和遮挡。 |
| [04_北京天坛_游客台阶](videos/04_北京天坛_游客台阶.mp4) | 北京天坛（来源页面标注） | 14.54秒 | 1920×1080 | 近距离游客台阶，来源标题注明慢动作；检查半身、遮挡，勿解释为真实运动速度。 |
| [05_上海传统旅游街区_游客人群](videos/05_上海传统旅游街区_游客人群.mp4) | 上海传统旅游街区（来源页面标注；具体机位未核实） | 33.27秒 | 1920×1080 | 人群密集、近距离遮挡、移动镜头；适合计数稳定性与异常检查。 |

建议先用长城片测试较稳定的通道画面，再用故宫、上海片测试遮挡和密集人群。天坛存在近距离半身人物和来源慢动作；黄山为新闻剪辑，有机位/镜头切换。可按视频相对秒数抽帧，不要把整段新闻片当作同一固定ROI的连续客流。

## 来源、许可和改动

公开许可作为本轮本地代码测试的使用依据；来源日期不是精确录制起止时间。所有视频未登记为业务系统中的正式录像，也没有填写虚构的授权记录或绝对录制时间。此批没有人工人数真值，未运行模型评测，不代表MAE或连续预测验收通过。

- **huangshan_snow**：作者 中国新闻网；[原始页面](https://commons.wikimedia.org/wiki/File:2019年1月1日_安徽黄山漫山银装宛如仙境.webm)；[CC BY 3.0](https://creativecommons.org/licenses/by/3.0/)。本地副本改动：H.264转码、必要时缩小、移除音轨，保留原播放速度。
- **great_wall**：作者 8K（Pexels页面署名）；[原始页面](https://www.pexels.com/video/people-in-the-great-wall-of-china-2881972/)；[Pexels License](https://www.pexels.com/license/)。本地副本改动：H.264转码、必要时缩小、移除音轨，保留原播放速度。
- **forbidden_city_courtyard**：作者 8K（Pexels页面署名）；[原始页面](https://www.pexels.com/video/people-walking-a-courtyard-of-a-palace-in-china-2953628/)；[Pexels License](https://www.pexels.com/license/)。本地副本改动：H.264转码、必要时缩小、移除音轨，保留原播放速度。
- **temple_of_heaven**：作者 8K（Pexels页面署名）；[原始页面](https://www.pexels.com/video/slow-motion-footage-of-people-traffic-in-a-temple-2894885/)；[Pexels License](https://www.pexels.com/license/)。本地副本改动：H.264转码、必要时缩小、移除音轨，保留原播放速度。
- **shanghai_tourist_market**：作者 Iyan Darmawan；[原始页面](https://www.pexels.com/video/crowd-of-tourists-walking-5234040/)；[Pexels License](https://www.pexels.com/license/)。本地副本改动：H.264转码、必要时缩小、移除音轨，保留原播放速度。

黄山署名：中国新闻网，视频《2019年1月1日 安徽黄山漫山银装宛如仙境》，经 Wikimedia Commons 获取，CC BY 3.0。请在转交或展示其截图/视频时保留作者、来源、许可及上述改动说明。Pexels素材同样保留作者和来源记录。

## 文件与自检

- `manifest.json`：五段的来源、许可、原件与MP4摘要、尺寸、时长和验证状态。
- `test_inputs.json`：相对路径输入列表；时间基准为视频内相对秒数。
- `previews/`：抽帧预览；不含人工人数标注，预览保留对应原视频许可。
- `sources/selected-source-records.json`：选中素材的来源记录。
- ZIP只包含测试MP4、预览、说明、清单和自检脚本；下载原件保留在本地 `originals/`，不重复塞进ZIP。

在本目录运行：

```bash
python verify_media.py
```

本次已实际对五个MP4从头到尾解码并核对SHA256。自检脚本只复核文件大小和摘要，不启动计数模型，不连接数据库。下载和整理脚本仅在临时工具目录执行，未改动爬虫或C/D/E业务代码。
