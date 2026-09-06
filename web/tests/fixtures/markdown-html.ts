/** A README header mixing ordinary Markdown with GitHub-style inline HTML. */
export const MARKDOWN_HTML_README = `<p align="center"><img src="https://preview.example/header.png" alt="Robot overview" width="820" height="320"></p>
<h1 align="center">Microduck</h1>
<p align="center"><a href="README.md">English</a> · <a href="readme_zh.md">简体中文</a></p>
<p align="center"><em>通过强化学习策略运动的小型双足机器人。</em></p>
<p align="center"><a href="https://preview.example/project">官方项目</a> · <a href="docs/install.md#L12">安装指南</a></p>

---

**这个仓库是机器人的“大脑”。**

<p align="left"><img src="./local-logo.png" alt="Local logo" width="64"></p>

<details><summary>安装说明</summary>

- 保留 **Markdown** 排版
- 本地链接仍通过 Remote 打开

</details>

<h2 id="installation">安装</h2>
<a href="#installation">跳转到安装</a>

| 检查 | 状态 |
| --- | --- |
| 预览 | 正常 |

- [x] 已检查

<script>document.body.dataset.mdUnsafe = 'script';</script>
<style>body { display: none !important; }</style>
<iframe src="https://preview.example/unsafe-frame"></iframe>
<form action="https://preview.example/unsafe-form"><button>不可提交</button></form>
<img src="javascript:alert(1)" onerror="document.body.dataset.mdUnsafe = 'event'" alt="Unsafe image">
<p style="position:fixed;inset:0" onclick="document.body.dataset.mdUnsafe = 'click'">安全文字</p>
<a href="javascript:alert(1)" target="_self" download>危险链接</a>
<picture><source srcset="/private/local.png 1x, https://preview.example/unsafe-source 2x"></picture>`;

// Browser layout checks cover both wrapper-supplied assets and the narrow
// GitHub allowlist, without depending on a real remote image or live network.
export const MARKDOWN_HTML_LOCAL_README = MARKDOWN_HTML_README.replace(
  "https://preview.example/header.png", "./header.svg",
);
export const GITHUB_README_ATTACHMENT_URL = "https://github.com/user-attachments/assets/00000000-0000-4000-8000-000000000001";
export const GITHUB_README_IMAGE_URL = "https://github-production-user-asset-6210df.s3.amazonaws.com/1/readme-test.svg";
export const MARKDOWN_HTML_GITHUB_README = MARKDOWN_HTML_README.replace(
  "https://preview.example/header.png", GITHUB_README_ATTACHMENT_URL,
);
export const MARKDOWN_HTML_HEADER_SVG = '<svg xmlns="http://www.w3.org/2000/svg" width="820" height="320" viewBox="0 0 820 320">'
  + '<rect width="820" height="320" fill="#edf2fc"/>'
  + '<rect x="350" y="55" width="120" height="110" rx="18" fill="#6482c3"/>'
  + '<path d="M380 165v65h-45m105-65v65h45" fill="none" stroke="#6482c3" stroke-width="18"/>'
  + '<text x="410" y="290" text-anchor="middle" font-family="sans-serif" font-size="22" fill="#30486c">Robot overview</text></svg>';
