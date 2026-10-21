# 验证方式与范围

在全新的 Python 3.12 虚拟环境中安装本仓库的 requirements.lock.txt，再运行 pip check 和完整 unittest：

~~~sh
python -m pip install -r requirements.lock.txt
python -m pip check
python -B -m unittest discover -s tests -v
~~~

测试使用临时 Vault、合成材料和显式替代的外部接口，覆盖分享自动路由、收藏夹解析、缓存与作品 ID 绑定、材料准备、笔记与附件、查重、复审、索引日志、事务回滚、安装配置和模块来源。测试只加载本仓库的脚本。

知识总索引和导入总日志由本仓库维护。日志摘要显示总记录数，历史来源单元格仍被保留。目录检查命令只读取当前 Vault 的这两份文件，不合并其他目录或维护额外视图。

离线回归、依赖安装与实际在线导入分别判断。单元测试不能证明抖音接口、账号权限或 Cookie 当前可用，也不能证明任意素材的转写和识别质量。doctor 只检查本机配置和依赖发现。

本文给出当前源码的验证方法；具体测试结果以对应提交或 Release 的运行记录为准。

当前业务脚本的 SHA-256 见 [源码指纹](core-source-sha256.json)。
