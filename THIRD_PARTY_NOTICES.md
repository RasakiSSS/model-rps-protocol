# 第三方依赖与许可声明

核查日期：2026-10-20。本项目自身采用 [MIT License](LICENSE)，第三方组件保留各自的版权和许可条款。

本项目通过 Python 包接口使用下面的依赖。当前 GitHub 源码包和本项目 wheel 不内置这些库的实现、Python 解释器或模型权重；安装时由包管理器另行获取依赖。以下清单记录已核查的发行版本，不作为依赖锁定文件。升级、启用额外依赖或制作包含依赖的安装包时，应按实际版本更新清单和声明。

## 直接运行依赖

| 项目与源码 | 核查版本 | 用途 | 许可证与原始声明 |
| --- | --- | --- | --- |
| [HTTPX](https://github.com/encode/httpx) | 0.28.1 | HTTP 请求与流式接收 | BSD-3-Clause；[声明](LICENSES/httpx-LICENSE.md) |
| [prompt-toolkit](https://github.com/prompt-toolkit/python-prompt-toolkit) | 3.0.52、3.0.53 | 交互终端与异步输入 | BSD-3-Clause；[声明](LICENSES/prompt-toolkit-LICENSE)、[作者](LICENSES/prompt-toolkit-AUTHORS.rst) |

prompt-toolkit 两个版本的许可与作者文件内容一致。

## 当前默认运行依赖带入的组件

| 项目与源码 | 核查版本 | 引入关系 | 许可证与原始声明 |
| --- | --- | --- | --- |
| [AnyIO](https://github.com/agronholm/anyio) | 4.15.1 | HTTPX | MIT；[声明](LICENSES/anyio-LICENSE) |
| [HTTPCore](https://github.com/encode/httpcore) | 1.0.9 | HTTPX | BSD-3-Clause；[声明](LICENSES/httpcore-LICENSE.md) |
| [certifi](https://github.com/certifi/python-certifi) | 2026.7.22 | HTTPX、HTTPCore | MPL-2.0；[声明](LICENSES/certifi-LICENSE) |
| [idna](https://github.com/kjd/idna) | 3.20 | HTTPX、AnyIO | BSD-3-Clause；[声明](LICENSES/idna-LICENSE.md) |
| [h11](https://github.com/python-hyper/h11) | 0.16.0 | HTTPCore | MIT；[声明](LICENSES/h11-LICENSE.txt) |
| [wcwidth](https://github.com/jquast/wcwidth) | 0.9.2 | prompt-toolkit | MIT，并保留上游 Markus Kuhn 的许可；[完整声明](LICENSES/wcwidth-LICENSE) |
| [typing_extensions](https://github.com/python/typing_extensions) | 4.16.0 | AnyIO，在 Python < 3.15 时需要 | PSF-2.0；[完整声明及历史许可](LICENSES/typing_extensions-LICENSE) |

## 可选 MCP 服务

`.[mcp]` 安装 [官方 Python SDK](https://github.com/modelcontextprotocol/python-sdk) 的 mcp 2.3.0，同时带入 mcp-types 2.3.0。本项目调用 SDK 的工具注册、生命周期与 stdio 传输接口，没有复制其实现；两个发行包的 MIT 许可文件内容一致，保留[原始版权与许可](LICENSES/mcp-LICENSE)。

SDK 的传递依赖由包管理器另行安装；本项目不内置 SDK、其依赖或 Agent 宿主。这里记录直接使用的可选组件，未扩展为全部传递依赖的许可清单。未来分发包含这些依赖的安装包时，应按实际包含版本核查并保留各自声明。

## 构建工具与运行环境

[setuptools](https://github.com/pypa/setuptools) 用于构建本项目，主体采用 MIT；核查版本为 84.0.0，保留其[原始许可文件](LICENSES/setuptools-LICENSE)。它内部携带的其他组件仍按各自条款授权；本项目不分发 setuptools 或其内部组件。

Python 解释器和标准库由用户安装，适用 [Python 自身的许可证](https://docs.python.org/3/license.html)。模型 API 的服务条款由相应提供方规定，不属于本项目 MIT 授权的范围。

## 声明的来源与适用范围

`LICENSES/` 中的文件直接复制自上述发行包的 `.dist-info/licenses/`，保留原始版权、许可条件、免责声明及作者信息，不以本项目 MIT 许可证替换。源码分发包和 wheel 通过 `pyproject.toml` 的 `license-files` 一并收录这些文件。

其中，certifi 的声明指向 [MPL-2.0 全文](https://www.mozilla.org/en-US/MPL/2.0/)。本项目调用未修改的 certifi，未将其受 MPL 约束的代码复制到本项目文件中。MPL 的文件级要求允许与其他许可证的独立文件共同使用，参见 [Mozilla FAQ 第 11 项](https://www.mozilla.org/en-US/MPL/2.0/FAQ/#q11-how-viral-is-the-mpl-if-i-use-mpl-licensed-code-in-my-proprietary-application-will-i-have-to-give-all-the-source-code-away)。本项目采用 MIT 不改变 certifi 的 MPL 授权。

如果后续分发包含依赖的安装包，需要保留实际打包版本的版权、许可证和免责声明；对 MPL 组件还应按条款提供相应源码获取方式。上面的清单与副本记录本次核查结果，不能替代实际分发版本自身的完整许可材料。
