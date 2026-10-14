import assert from "node:assert/strict";
import {readFile} from "node:fs/promises";
import test from "node:test";
import vm from "node:vm";
const source=await readFile(new URL("../public/console/app.js",import.meta.url),"utf8");
const block=source.slice(source.indexOf("  function corpusContextHtml("),source.indexOf("  function renderCorpusChat("));
const render=vm.runInNewContext(`${block}\ncorpusContextHtml`,{escapeHtml:v=>String(v).replaceAll("&","&amp;").replaceAll("<","&lt;").replaceAll('"',"&quot;")});
test("retrieved chunk presents post title and separate original page/file links",()=>{
 const html=render({rank:1,title:"<Post>",text:"evidence",websiteUrl:"https://example.org/post",driveUri:"https://drive.google.com/file/d/f/view",sourceUri:"gs://bucket/f.md"});
 assert.match(html,/&lt;Post>/);assert.match(html,/게시글 원문 ↗/);assert.match(html,/원본 파일 ↗/);
 assert.match(html,/rel="noopener noreferrer"/);
});
test("untrusted non-web links never become clickable citations",()=>{
 const html=render({rank:1,text:"text",websiteUrl:"javascript:alert(1)",driveUri:"https://evil.example/file"});
 assert.doesNotMatch(html,/<a /);
});
