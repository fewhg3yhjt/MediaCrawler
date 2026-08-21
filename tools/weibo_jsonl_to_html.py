# -*- coding: utf-8 -*-
"""将微博 jsonl 数据转换为自包含单文件 HTML 时间线归档站。

用法:
    python tools/weibo_jsonl_to_html.py \
        --input data/weibo/jsonl/creator_contents_*.jsonl \
        --output data/weibo/tombkeeper_weibo.html \
        --title "tombkeeper 微博存档"

特性:
    - 全部数据内嵌为 JSON，双击 HTML 即用，离线可用，无服务器依赖
    - 按 年 > 月 分组，顶部年份导航快速跳转
    - 懒渲染：滚动到对应年份才渲染该年微博（支持 2.5 万条级别）
    - 实时全文搜索（中文子串匹配，多关键词空格分隔，毫秒级）
    - 图片懒加载、暗色/亮色切换
"""
import argparse
import glob
import json
import os
import pathlib
import shutil
import sys
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from typing import Dict, List, Optional

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass


def parse_image_urls(value) -> List[str]:
    """image_urls 可能是 list 或 str(内含 JSON 数组)。"""
    if not value:
        return []
    if isinstance(value, list):
        return [str(u) for u in value if str(u).startswith("http")]
    if isinstance(value, str):
        try:
            data = json.loads(value)
            if isinstance(data, list):
                return [str(u) for u in data if str(u).startswith("http")]
        except Exception:
            pass
    return []


def load_posts(input_paths: List[str]) -> List[Dict]:
    seen: Dict[str, Dict] = {}
    for path in input_paths:
        for line in open(path, encoding="utf-8"):
            line = line.strip()
            if not line:
                continue
            d = json.loads(line)
            if not d.get("note_id"):
                continue
            seen[d["note_id"]] = d
    posts = list(seen.values())
    posts.sort(key=lambda p: int(p.get("create_time", 0) or 0), reverse=True)
    return posts


def build_payload(posts: List[Dict], url_map: Optional[Dict[str, str]] = None) -> List[Dict]:
    payload = []
    for p in posts:
        images = []
        for u in parse_image_urls(p.get("image_urls")):
            local = (url_map or {}).get(u)
            images.append({"u": local if local else u, "l": 1 if local else 0})
        payload.append({
            "id": p.get("note_id", ""),
            "t": int(p.get("create_time", 0) or 0),
            "dt": p.get("create_date_time", ""),
            "c": p.get("content", "") or "",
            "nick": p.get("nickname", "") or "",
            "r": p.get("is_retweet", "0") == "1",
            "rurl": p.get("retweeted_original_url", "") or "",
            "l": str(p.get("liked_count", "0")),
            "cc": str(p.get("comments_count", "0")),
            "sh": str(p.get("shared_count", "0")),
            "url": p.get("note_url", "") or "",
            "ourl": p.get("original_url", "") or "",
            "mt": p.get("media_type", "") or "",
            "imgs": images,
            "vurl": p.get("video_url", "") or "",
            "ip": p.get("ip_location", "") or "",
        })
    return payload


def build_years(posts: List[Dict]) -> List[Dict]:
    years: Dict[str, List[int]] = {}
    for p in posts:
        year = str(p.get("create_date_time", ""))[:4] or "未知"
        years.setdefault(year, []).append(int(p.get("create_time", 0) or 0))
    result = []
    for year in sorted(years.keys(), reverse=True):
        ts = sorted(years[year])
        import datetime
        start = datetime.datetime.fromtimestamp(ts[0]).strftime("%Y-%m-%d")
        end = datetime.datetime.fromtimestamp(ts[-1]).strftime("%Y-%m-%d")
        result.append({"year": year, "count": len(ts), "start": start, "end": end})
    return result


TEMPLATE = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{{TITLE}}</title>
<style>
:root{
  --bg:#f5f6f8; --card:#ffffff; --line:#e6e8eb; --text:#1f2329;
  --muted:#8a919f; --accent:#e2492d; --accent2:#ff8200; --chip:#f2f3f5;
  --hover:#fafbfc; --bar:#1f2329; --bar-text:#ffffff; --input-bg:#ffffff;
}
html[data-theme="dark"]{
  --bg:#14161a; --card:#1d2026; --line:#2c313a; --text:#e7eaef;
  --muted:#8b93a1; --accent:#ff6b4a; --accent2:#ff9a3d; --chip:#262b33;
  --hover:#242932; --bar:#101216; --bar-text:#e7eaef; --input-bg:#262b33;
}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--text);
  font:15px/1.7 -apple-system,"Segoe UI","Microsoft YaHei",sans-serif}
a{color:var(--accent);text-decoration:none}
a:hover{text-decoration:underline}
header{position:sticky;top:0;z-index:10;background:var(--bar);color:var(--bar-text);
  padding:10px 18px;box-shadow:0 2px 8px rgba(0,0,0,.15)}
.top{display:flex;align-items:center;gap:14px;flex-wrap:wrap}
h1{font-size:18px;margin:0;font-weight:600;white-space:nowrap}
.stats{font-size:12px;opacity:.85;white-space:nowrap}
.search{flex:1;min-width:220px;display:flex;align-items:center;gap:8px}
#q{flex:1;padding:8px 12px;border:1px solid var(--line);border-radius:8px;
  background:var(--input-bg);color:var(--text);font-size:14px;outline:none}
#q:focus{border-color:var(--accent)}
button.theme{background:var(--chip);border:none;border-radius:8px;color:var(--text);
  padding:8px 12px;cursor:pointer;font-size:13px}
.nav{display:flex;gap:6px;margin-top:10px;overflow-x:auto;padding-bottom:2px;scrollbar-width:thin}
.nav a{color:var(--bar-text);opacity:.9;font-size:13px;padding:3px 10px;border-radius:6px;
  background:rgba(255,255,255,.08);white-space:nowrap}
.nav a:hover{opacity:1;background:rgba(255,255,255,.18);text-decoration:none}
main{max-width:760px;margin:0 auto;padding:18px 14px 60px}
.month{font-size:16px;font-weight:700;margin:26px 0 10px;padding-left:10px;
  border-left:3px solid var(--accent);color:var(--text)}
.month small{font-weight:400;color:var(--muted);margin-left:8px;font-size:13px}
.post{background:var(--card);border:1px solid var(--line);border-radius:12px;
  padding:14px 16px;margin:10px 0;transition:background .15s}
.post:hover{background:var(--hover)}
.post-head{display:flex;align-items:center;gap:8px;color:var(--muted);font-size:13px;margin-bottom:8px}
.post-head .who{color:var(--text);font-weight:600}
.tag{font-size:11px;padding:1px 7px;border-radius:999px;background:var(--chip);color:var(--muted)}
.tag.rt{background:rgba(226,73,45,.12);color:var(--accent)}
.post-content{white-space:pre-wrap;word-break:break-word;margin:0 0 10px}
.imgs{display:grid;grid-template-columns:repeat(auto-fill,minmax(150px,1fr));gap:6px;margin:8px 0}
.imgs img{width:100%;border-radius:8px;border:1px solid var(--line);background:#eee;display:block}
.imgs a.vid{display:flex;align-items:center;gap:6px;color:var(--accent);font-size:13px}
.post-foot{display:flex;gap:16px;flex-wrap:wrap;color:var(--muted);font-size:13px}
.post-foot span{white-space:nowrap}
.post-foot a{margin-left:auto}
.retweet{background:var(--chip);border-radius:8px;padding:8px 10px;margin-top:8px;font-size:14px}
.retweet a{font-size:12px}
.year-sec{scroll-margin-top:96px}
#results-title{font-weight:600;margin:14px 0 6px}
#results-title small{color:var(--muted);font-weight:400}
.more-hint{text-align:center;color:var(--muted);font-size:13px;padding:16px}
footer{color:var(--muted);font-size:12px;text-align:center;padding:20px}
</style>
</head>
<body>
<header>
  <div class="top">
    <h1>{{TITLE}}</h1>
    <span class="stats">{{TOTAL}} 条微博 · {{RANGE}}</span>
    <div class="search">
      <input id="q" type="search" placeholder="搜索正文/时间/链接，多关键词用空格分隔">
      <button class="theme" id="themeBtn" title="切换亮/暗色">🌓</button>
    </div>
  </div>
  <nav class="nav" id="yearNav"></nav>
</header>
<main id="main">
  <div id="searchView" style="display:none"></div>
  <div id="timelineView"></div>
</main>
<footer>由 MediaCrawler 抓取生成 · tombkeeper 微博存档</footer>
<script>
const DATA = {{DATA}};
const YEARS = {{YEARS}};
</script>
<script>
"use strict";
const posts = DATA;
const years = YEARS;
const timelineEl = document.getElementById('timelineView');
const searchEl = document.getElementById('searchView');
const navEl = document.getElementById('yearNav');
const qEl = document.getElementById('q');

const monthNames = ['1月','2月','3月','4月','5月','6月','7月','8月','9月','10月','11月','12月'];

// ---------- 渲染单条微博 ----------
function esc(s){return String(s==null?'':s);}

function makePost(p){
  const box=document.createElement('div');
  box.className='post';
  const head=document.createElement('div');
  head.className='post-head';
  head.innerHTML='<span class="who">'+esc(p.nick||'未知')+'</span><span>'+esc(p.dt)+'</span>';
  if(p.ip){const s=document.createElement('span');s.textContent=esc(p.ip);head.appendChild(s);}
  if(p.r){const s=document.createElement('span');s.className='tag rt';s.textContent='转发';head.appendChild(s);}
  const mtMap={image:'图片',video:'视频',webpage:'链接',search_topic:'话题'};
  if(mtMap[p.mt]){const s=document.createElement('span');s.className='tag';s.textContent=mtMap[p.mt];head.appendChild(s);}
  box.appendChild(head);
  const body=document.createElement('div');
  body.className='post-content';
  body.textContent=p.c;
  box.appendChild(body);
  if(p.imgs&&p.imgs.length){
    const g=document.createElement('div');g.className='imgs';
    p.imgs.forEach(it=>{
      const a=document.createElement('a');
      a.href=it.u; a.target='_blank'; a.rel='noopener';
      const img=document.createElement('img');
      img.loading='lazy'; img.referrerPolicy='no-referrer'; img.alt='微博配图';
      img.src=it.u;
      img.onerror=function(){const t=document.createElement('span');t.className='tag';t.textContent='图片加载失败';this.replaceWith(t);};
      a.appendChild(img); g.appendChild(a);
    });
    box.appendChild(g);
  }
  if(p.vurl){
    const v=document.createElement('video');
    v.controls=true; v.preload='none'; v.style.maxWidth='100%'; v.style.borderRadius='8px';
    v.src=p.vurl; box.appendChild(v);
  }
  const foot=document.createElement('div');
  foot.className='post-foot';
  foot.innerHTML='<span>赞 '+esc(p.l)+'</span><span>评论 '+esc(p.cc)+'</span><span>转发 '+esc(p.sh)+'</span>';
  const a=document.createElement('a');
  a.href=p.url||'#';
  a.target='_blank'; a.rel='noopener'; a.textContent='原文 ↗';
  foot.appendChild(a);
  box.appendChild(foot);
  if(p.r&&p.rurl){
    const rt=document.createElement('div');
    rt.className='retweet';
    rt.innerHTML='转发自: <a href="'+esc(p.rurl)+'" target="_blank" rel="noopener">'+esc(p.rurl)+'</a>';
    box.appendChild(rt);
  }
  return box;
}

// ---------- 时间线（按年懒渲染） ----------
const renderedYears={};
const chunkSize=400;

function appendPosts(container, list, start, limit){
  const end=Math.min(start+limit, list.length);
  const frag=document.createDocumentFragment();
  for(let i=start;i<end;i++) frag.appendChild(makePost(list[i]));
  container.appendChild(frag);
  return end;
}

function renderYearSection(year){
  if(renderedYears[year]) return;
  renderedYears[year]=true;
  const list=posts.filter(p=>String(p.dt).slice(0,4)===year);
  if(!list.length) return;
  const section=document.createElement('section');
  section.className='year-sec';
  section.id='y'+year;
  const h=document.createElement('h2');
  h.style.cssText='margin:30px 0 4px;font-size:20px';
  h.textContent=year+' 年';
  section.appendChild(h);
  const meta=document.createElement('div');
  meta.style.cssText='color:var(--muted);font-size:13px;margin-bottom:4px';
  const y=YEARS.find(x=>x.year===year);
  meta.textContent=(y?('共 '+y.count+' 条 · '+y.start+' ~ '+y.end):('共 '+list.length+' 条'));
  section.appendChild(meta);
  // 月份分组容器
  const body=document.createElement('div');
  const byMonth={};
  list.forEach(p=>{
    const m=String(p.dt).slice(0,7);
    (byMonth[m]=byMonth[m]||[]).push(p);
  });
  const months=Object.keys(byMonth).sort().reverse();
  months.forEach(m=>{
    const mh=document.createElement('div');
    mh.className='month';
    mh.textContent=parseInt(m.slice(5))+'月';
    mh.innerHTML+=' <small>'+byMonth[m].length+' 条</small>';
    body.appendChild(mh);
    const wrap=document.createElement('div');
    body.appendChild(wrap);
    let done=appendPosts(wrap, byMonth[m], 0, chunkSize);
    if(done<byMonth[m].length){
      const more=document.createElement('div');
      more.className='more-hint';
      more.textContent='+ 加载更多';
      more.style.cssText='cursor:pointer;color:var(--accent)';
      more.onclick=()=>{
        done=appendPosts(wrap, byMonth[m], done, chunkSize);
        if(done>=byMonth[m].length) more.style.display='none';
      };
      body.appendChild(more);
    }
  });
  section.appendChild(body);
  timelineEl.appendChild(section);
}

// 年份导航
YEARS.forEach(y=>{
  const a=document.createElement('a');
  a.href='#y'+y.year;
  a.textContent=y.year+' ('+y.count+')';
  a.addEventListener('click',()=>{
    if(qEl.value.trim()){qEl.value='';showSearch('');}
    renderYearSection(y.year);
  });
  navEl.appendChild(a);
});

// 懒渲染：可见的年份节才渲染
if('IntersectionObserver' in window){
  const obs=new IntersectionObserver(es=>{
    es.forEach(e=>{
      if(e.isIntersecting) renderYearSection(e.target.dataset.year);
    });
  },{rootMargin:'400px'});
  // 先为所有年份放占位节，再观察
  years.forEach(y=>{
    const holder=document.createElement('section');
    holder.className='year-sec';
    holder.id='y'+y.year;
    holder.dataset.year=y.year;
    timelineEl.appendChild(holder);
    obs.observe(holder);
  });
}else{
  years.forEach(y=>renderYearSection(y.year));
}

// 初始渲染最近一年
const firstYear=YEARS.length?YEARS[0].year:null;
if(firstYear) renderYearSection(firstYear);

// ---------- 搜索 ----------
let lastQuery='';
function showSearch(q){
  if(!q){
    searchEl.style.display='none';
    searchEl.innerHTML='';
    timelineEl.style.display='';
    return;
  }
  const words=q.toLowerCase().split(/\s+/).filter(Boolean);
  const hit=posts.filter(p=>{
    const hay=(p.c+' '+p.dt+' '+p.id+' '+(p.ourl||'')).toLowerCase();
    return words.every(w=>hay.includes(w));
  });
  searchEl.innerHTML='';
  const title=document.createElement('div');
  title.id='results-title';
  title.innerHTML='搜索结果：'+hit.length+' 条 <small>（匹配「'+esc(q)+'」）</small>';
  searchEl.appendChild(title);
  const wrap=document.createElement('div');
  searchEl.appendChild(wrap);
  let done=appendPosts(wrap, hit, 0, 200);
  if(done<hit.length){
    const more=document.createElement('div');
    more.className='more-hint';
    more.style.cssText='cursor:pointer;color:var(--accent)';
    more.textContent='+ 加载更多结果 ('+(hit.length-done)+')';
    more.onclick=()=>{
      done=appendPosts(wrap, hit, done, 500);
      if(done>=hit.length) more.style.display='none';
      else more.textContent='+ 加载更多结果 ('+(hit.length-done)+')';
    };
    searchEl.appendChild(more);
  }
  searchEl.style.display='';
  timelineEl.style.display='none';
}

let debounceTimer;
qEl.addEventListener('input',()=>{
  clearTimeout(debounceTimer);
  debounceTimer=setTimeout(()=>{
    const q=qEl.value.trim();
    if(q!==lastQuery){lastQuery=q;showSearch(q);}
  },180);
});

// 主题切换
const themeBtn=document.getElementById('themeBtn');
const saved=localStorage.getItem('wb-theme');
if(saved) document.documentElement.dataset.theme=saved;
themeBtn.addEventListener('click',()=>{
  const cur=document.documentElement.dataset.theme==='dark'?'':'dark';
  document.documentElement.dataset.theme=cur;
  localStorage.setItem('wb-theme',cur);
});
</script>
</body>
</html>
"""


DOWNLOAD_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"


def _image_filename(url: str) -> str:
    path = urllib.parse.urlsplit(url).path
    name = os.path.basename(path)
    if not name:
        import hashlib
        name = hashlib.md5(url.encode()).hexdigest() + ".jpg"
    return name


def _download_one(url: str, dest: pathlib.Path) -> bool:
    if dest.exists() and dest.stat().st_size > 0:
        return True
    tmp = dest.with_name(dest.name + ".part")
    for attempt in range(3):
        try:
            req = urllib.request.Request(
                url,
                headers={"User-Agent": DOWNLOAD_UA, "Referer": "https://weibo.com/"},
            )
            with urllib.request.urlopen(req, timeout=30) as resp, open(tmp, "wb") as f:
                shutil.copyfileobj(resp, f)
            if tmp.stat().st_size > 0:
                tmp.replace(dest)
                return True
        except Exception as e:
            if attempt == 2:
                print(f"  下载失败: {url} ({type(e).__name__})")
    return False


def download_images(posts: List[Dict], images_dir: pathlib.Path, workers: int) -> Dict[str, str]:
    """下载所有微博图片到本地，返回 原URL -> 本地相对路径(相对 HTML 所在目录) 映射。"""
    images_dir.mkdir(parents=True, exist_ok=True)
    unique: List[str] = []
    seen = set()
    for p in posts:
        for u in parse_image_urls(p.get("image_urls")):
            if u not in seen:
                seen.add(u)
                unique.append(u)
    print(f"开始下载图片: {len(unique)} 张 → {images_dir}")
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futures = {ex.submit(_download_one, u, images_dir / _image_filename(u)): u for u in unique}
        done = 0
        url_map: Dict[str, str] = {}
        for f, u in futures.items():
            if f.result():
                url_map[u] = "images/" + _image_filename(u)
            done += 1
            if done % 20 == 0 or done == len(unique):
                print(f"  进度: {done}/{len(unique)}")
    print(f"图片下载完成: 成功 {len(url_map)}/{len(unique)}")
    return url_map


def main() -> None:
    parser = argparse.ArgumentParser(description="jsonl -> 单文件 HTML 时间线归档")
    parser.add_argument("--input", nargs="+", required=True, help="输入的 jsonl 文件(可多个)")
    parser.add_argument("--output", required=True, help="输出 HTML 路径")
    parser.add_argument("--title", default="tombkeeper 微博存档", help="页面标题")
    parser.add_argument("--download-images", action="store_true", help="下载图片到本地并内嵌引用")
    parser.add_argument("--images-dir", default="data/weibo/images", help="图片存放目录(默认 data/weibo/images)")
    parser.add_argument("--workers", type=int, default=4, help="图片下载并发数(默认 4)")
    args = parser.parse_args()

    input_paths = []
    for pat in args.input:
        # 用标准 glob 同时支持相对/绝对路径和通配符
        matches = sorted(pathlib.Path(p) for p in glob.glob(pat))
        if not matches:
            print(f"警告: 无文件匹配 {pat}")
        input_paths.extend(matches)
    if not input_paths:
        sys.exit("没有找到输入文件")

    posts = load_posts([str(p) for p in input_paths])
    if not posts:
        sys.exit("没有有效微博数据")

    url_map: Dict[str, str] = {}
    if args.download_images:
        url_map = download_images(posts, pathlib.Path(args.images_dir), args.workers)

    payload = build_payload(posts, url_map)
    years = build_years(posts)

    start = min(int(p["create_time"] or 0) for p in posts)
    end = max(int(p["create_time"] or 0) for p in posts)
    import datetime
    range_str = f"{datetime.datetime.fromtimestamp(start).strftime('%Y-%m-%d')} ~ {datetime.datetime.fromtimestamp(end).strftime('%Y-%m-%d')}"

    data_json = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/")
    years_json = json.dumps(years, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/")

    html = (TEMPLATE
            .replace("{{TITLE}}", args.title)
            .replace("{{TOTAL}}", str(len(posts)))
            .replace("{{RANGE}}", range_str)
            .replace("{{DATA}}", data_json)
            .replace("{{YEARS}}", years_json))

    out = pathlib.Path(args.output)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(html, encoding="utf-8")
    print(f"完成: {len(posts)} 条微博 → {out} ({out.stat().st_size/1024/1024:.1f} MB)")


if __name__ == "__main__":
    main()
