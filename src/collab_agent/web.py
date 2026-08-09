from __future__ import annotations

import json
import os
import re
import socket
from copy import deepcopy
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any
from urllib.parse import parse_qs, urlparse

from .auth import (
    AuthorizationService,
    Principal,
    PrincipalError,
    VirtualSessionPrincipalProvider,
)
from .attachments import MAX_ATTACHMENT_COUNT, MAX_TOTAL_ATTACHMENT_BYTES
from .metrics import build_report
from .memory_lexicon import memory_lexicon_payload
from .models import ASSIGNMENT_RETURN_REASONS, OTHER_RETURN_REASON, parse_time


# Base64 inflates the attachment ceiling by ~4/3; the rest covers JSON framing
# and the text fields that travel with a delivery.
MAX_REQUEST_BYTES = MAX_TOTAL_ATTACHMENT_BYTES * 4 // 3 + 1024 * 1024


class RequestTooLarge(Exception):
    """The request body exceeds the ceiling and was never read into memory."""


class SingleInstanceHTTPServer(HTTPServer):
    """Bind a workbench port exclusively, including on Windows."""

    allow_reuse_address = False

    def server_bind(self) -> None:
        if os.name == "nt" and hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            self.socket.setsockopt(
                socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1
            )
        super().server_bind()
from .service import NOTIFICATION_EFFECT_TYPES, CoordinationService


WORKBENCH_HTML = r"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>办公协作 Agent</title>
<style>
:root{color-scheme:light;--bg:#f5f3ee;--ink:#17211b;--muted:#667169;--card:#fff;--line:#d9ddd8;--ok:#1d7a4a;--bad:#a33b32;--warn:#a66714;--accent:#2457a7;--collab:#7650a8;--soft:#eef2ef}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:14px/1.55 system-ui,"Microsoft YaHei",sans-serif}button,input,textarea,select{font:inherit}
header{background:#fff;border-bottom:1px solid var(--line);position:sticky;top:0;z-index:5}.bar{max-width:1180px;margin:auto;padding:14px 24px;display:flex;align-items:center;gap:22px}.brand{font-weight:800;font-size:17px}.nav{display:flex;gap:5px}.nav a{color:var(--muted);text-decoration:none;padding:7px 10px;border-radius:7px}.nav a.active{background:var(--soft);color:var(--ink);font-weight:700}.identity{display:flex;align-items:center;gap:7px}.identity select{width:180px}.assignment-notice{margin-left:auto;position:relative}.bell{position:relative;width:38px;height:38px;border-radius:50%;padding:0;background:var(--soft);color:var(--ink);font-size:18px}.bell-count{position:absolute;right:-4px;top:-5px;min-width:18px;height:18px;padding:0 5px;border-radius:9px;background:var(--bad);color:#fff;font-size:11px;line-height:18px}.assignment-popover{position:absolute;right:0;top:46px;width:min(390px,calc(100vw - 32px));max-height:520px;overflow:auto;background:#fff;border:1px solid var(--line);border-radius:12px;box-shadow:0 16px 40px #1020182e;padding:12px;z-index:20}.assignment-popover .card{box-shadow:none;margin-top:8px}
main{max-width:1180px;margin:auto;padding:26px 24px 60px}.top{display:flex;justify-content:space-between;gap:18px;align-items:end;margin-bottom:20px}.eyebrow{color:var(--accent);font-weight:700;letter-spacing:.08em;font-size:12px}.muted{color:var(--muted)}h1{font-size:27px;margin:4px 0}h2{font-size:18px;margin:0 0 13px}h3{font-size:15px;margin:0 0 8px}.section{margin-top:18px}.grid{display:grid;gap:14px}.two{grid-template-columns:1fr 1fr}.gates{grid-template-columns:repeat(5,1fr)}.card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:16px;box-shadow:0 2px 10px #1020180a}.task{border-left:4px solid var(--accent)}.task.done{border-left-color:var(--ok)}.task.blocked{border-left-color:var(--warn)}.gate{border-top:4px solid var(--ok)}.gate.fail{border-top-color:var(--bad)}
.row{display:flex;gap:8px;align-items:center;flex-wrap:wrap}.between{justify-content:space-between}.pill{display:inline-block;padding:2px 8px;border-radius:999px;background:var(--soft);font-size:12px}.pill.warn{background:#fff0d8;color:#80500b}.pill.ok{background:#e5f5eb;color:#12643c}.pill.bad{background:#fbe8e6;color:#8a2d25}.meta{display:flex;gap:12px;flex-wrap:wrap;color:var(--muted);font-size:12px;margin:7px 0}.quote{background:#f7f7f4;border-radius:8px;padding:9px;margin-top:8px;color:var(--muted)}details{margin-top:8px}label{display:block;font-size:12px;color:var(--muted);margin:8px 0 3px}input,textarea,select{width:100%;border:1px solid var(--line);border-radius:7px;padding:8px 9px;background:#fff;color:var(--ink)}textarea{min-height:72px;resize:vertical}.form-grid{display:grid;grid-template-columns:1fr 1fr;gap:8px}.form-grid .wide{grid-column:1/-1}.actions{display:flex;gap:8px;flex-wrap:wrap;margin-top:10px}button{border:0;border-radius:7px;padding:8px 12px;background:var(--accent);color:white;cursor:pointer}button.secondary{background:#68736c}button.danger{background:#8a3c35}button.good{background:var(--ok)}button:disabled{opacity:.55;cursor:not-allowed}.empty{padding:24px;text-align:center;color:var(--muted);border:1px dashed var(--line);border-radius:10px}.event{padding:9px 0;border-bottom:1px solid var(--line)}pre{white-space:pre-wrap;margin:0;font-size:12px}.notice{padding:10px 12px;border-radius:8px;background:#edf3fb;color:#264f84;margin-bottom:14px}.error{background:#fbe8e6;color:#8a2d25}.hidden{display:none!important}
.activity-list{margin-top:8px;border-top:1px solid var(--line)}.activity-item{padding:10px 0;border-bottom:1px solid var(--line)}.activity-item:last-child{border-bottom:0}.activity-detail{margin-top:3px;color:var(--muted)}.timeline{display:grid;gap:9px}.timeline-row{display:grid;grid-template-columns:minmax(180px,1.15fr) minmax(260px,2fr);gap:12px;align-items:center;border:2px solid transparent;border-radius:10px;padding:10px 12px;background:#fff;cursor:pointer;transition:opacity .16s,border-color .16s,box-shadow .16s}.timeline-row.responsible{--role-color:var(--accent)}.timeline-row.collaboration{--role-color:var(--collab)}.timeline-row.selected{border-color:var(--role-color);box-shadow:0 0 0 2px color-mix(in srgb,var(--role-color) 14%,transparent)}.timeline.has-selection .timeline-row:not(.selected){opacity:.35}.timeline-bar{height:12px;border-radius:6px;background:color-mix(in srgb,var(--role-color) 17%,white);overflow:hidden;border:1px solid color-mix(in srgb,var(--role-color) 45%,white)}.timeline-fill{height:100%;background:var(--role-color);border-radius:6px}.timeline-fill.waiting{background:repeating-linear-gradient(45deg,var(--role-color) 0 6px,transparent 6px 11px)}.task-focus{margin-top:14px}.task-focus.responsible>.task{border-left-color:var(--accent)}.task-focus.collaboration>.task{border-left-color:var(--collab)}
@media(max-width:850px){.two,.gates,.form-grid{grid-template-columns:1fr}.form-grid .wide{grid-column:auto}.bar{align-items:flex-start;flex-wrap:wrap}.identity{margin-left:0;width:100%}.top{align-items:start;flex-direction:column}}
</style></head>
<body><header><div class="bar"><div class="brand">办公协作 Agent</div><nav class="nav"><a href="/tasks" data-route="tasks">同事任务</a><a href="/manage" data-route="manage">负责人</a><a href="/diagnostics" data-route="diagnostics">诊断</a></nav><div class="assignment-notice" id="assignment-notice"></div><div class="identity" id="identity"><span class="muted">虚拟身份</span><select id="current-actor"><option value="">请选择本次参会者</option></select></div></div></header>
<main><div id="flash"></div><div id="app"><div class="empty">正在载入…</div></div></main>
<script>
const route=location.pathname.split('/')[1]||'tasks';
document.querySelectorAll('[data-route]').forEach(a=>a.classList.toggle('active',a.dataset.route===route));
const esc=s=>String(s??'—').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const uid=()=>globalThis.crypto?.randomUUID?.()||String(Date.now())+'-'+Math.random();
let state=null;
let selectedTaskId=null;
let drafts=JSON.parse(localStorage.getItem('collabDrafts')||'{}');
const MAX_ATTACHMENT_COUNT=__MAX_ATTACHMENT_COUNT__;
const ATTACHMENT_LIMIT_MB=__ATTACHMENT_LIMIT_MB__;
const ATTACHMENT_LIMIT_BYTES=ATTACHMENT_LIMIT_MB*1024*1024;
const pendingFiles={};
const actorSelect=document.querySelector('#current-actor');
let sessionToken=localStorage.getItem('collabSessionToken')||'';
async function issueSession(actorId){const response=await fetch('/api/session',{method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify({actor_id:actorId})});const result=await response.json();if(!response.ok)throw new Error(result.message||'身份切换失败');sessionToken=result.token;localStorage.setItem('collabSessionToken',sessionToken);localStorage.setItem('collabActorId',actorId)}
actorSelect.addEventListener('change',async()=>{const actorId=actorSelect.value;if(!actorId)return;try{await issueSession(actorId);await load(true)}catch(e){flash(e.message,true)}});
document.addEventListener('input',e=>{if(e.target.dataset?.draft){drafts[e.target.dataset.draft]=e.target.type==='checkbox'?e.target.checked:e.target.value;localStorage.setItem('collabDrafts',JSON.stringify(drafts))}});
const RETURN_REASONS=__RETURN_REASONS__;
const OTHER_REASON=__OTHER_REASON__;
const draft=(key,fallback='')=>drafts[key]??fallback;
const field=key=>{const el=document.querySelector(`[data-draft="${key}"]`);return el?(el.type==='checkbox'?el.checked:el.value):draft(key)};
function clearDraftPrefix(prefix){Object.keys(drafts).filter(k=>k.startsWith(prefix)).forEach(k=>delete drafts[k]);localStorage.setItem('collabDrafts',JSON.stringify(drafts))}
function flash(message,error=false){document.querySelector('#flash').innerHTML=`<div class="notice ${error?'error':''}">${esc(message)}</div>`;setTimeout(()=>document.querySelector('#flash').innerHTML='',4500)}
const authHeaders=()=>sessionToken?{'authorization':`Bearer ${sessionToken}`}:{};
async function post(url,body){if(!sessionToken)throw new Error('请先选择本次会议身份');const response=await fetch(url,{method:'POST',headers:{'content-type':'application/json',...authHeaders()},body:JSON.stringify(body)});const result=await response.json();if(!response.ok)throw new Error(result.message||'操作失败');return result}
function phase(t){if(t.status==='REJECTED')return '已忽略';if(t.status==='PENDING_ASSIGNMENT')return '等待成员响应';if(t.status==='NEEDS_REVISION')return '退回重改';if(t.collaboration_progress&&!t.collaboration_progress.dependencies_ready)return '等待上游';if(!t.published_sim_time&&!t.owner_actor_id)return '待复核';if(t.collaboration_progress?.ballot_open&&!t.collaboration_progress?.vote_summary?.complete)return '等待投票';return ({TRACKING:'进行中',PENDING_ACCEPTANCE:'待验收',ACCEPTED:'已完成',AGGREGATED:'已汇总',ARCHIVED:'已归档'}[t.status]||t.status)}
function collaborationPanel(t){const p=t.collaboration_progress;if(!p)return '';const deps=p.dependencies||[],votes=p.vote_summary,ballot=(p.contributions||[]).find(x=>x.contribution_type==='BALLOT'&&x.status==='SUBMITTED'),draftBallot=(p.contributions||[]).find(x=>x.contribution_type==='BALLOT'&&x.status==='PENDING'&&x.payload?.draft),me=state?.principal?.actor_id,myVote=(p.contributions||[]).find(x=>x.contribution_type==='VOTE'&&x.actor_id===me),waiting=deps.filter(x=>!x.satisfied||x.stale);let ownerControls='';if(t.is_mine&&p.dependencies_ready&&!p.ballot_open){const generation=draftBallot?.payload?.generation||{},draftOptions=draftBallot?.payload?.options||[];ownerControls=`<details open><summary>Agent 整理候选问题并发起投票</summary><div class="muted">只读取已由会议负责人验收的上游结果。语义去重使用 qwen-plus；失败且规则能可靠抽取时会降级，不让流程中断。</div>${draftOptions.length?`<div class="meta"><span>草稿 ${draftOptions.length} 项</span><span>${esc(generation.model||generation.generation_mode||'确定性规则')}</span><span>Prompt ${esc(generation.prompt_version||'')}</span></div>${draftOptions.map((o,i)=>`<div class="activity-item"><label class="row"><input style="width:auto" type="checkbox" checked data-ballot-include="${esc(t.action_item_id)}" data-option-id="${esc(o.option_id)}" data-source-action-id="${esc(o.source_action_item_id)}" data-source-refs="${esc(encodeURIComponent(JSON.stringify(o.source_refs||[])))}"><input data-ballot-text="${esc(t.action_item_id)}" data-option-id="${esc(o.option_id)}" value="${esc(o.text)}"></label><div class="muted">来源 ${esc((o.source_refs||[]).map(x=>x.action_item_id).join('、'))}</div></div>`).join('')}<div class="actions"><button class="good" onclick="openBallot('${t.action_item_id}')">确认草稿并发布投票</button><button class="secondary" onclick="prepareBallotDraft('${t.action_item_id}')">重新整理</button></div>`:`<div class="actions"><button onclick="prepareBallotDraft('${t.action_item_id}')">从已验收结果生成投票草稿</button></div>`}</details>`}let voteControls='';if(ballot&&myVote&&myVote.status!=='SUBMITTED'){voteControls=`<details open><summary>给候选问题打分</summary>${(ballot.payload?.options||[]).map(o=>`<div class="row between"><span>${esc(o.text)}</span><select style="width:90px" data-vote-task="${esc(t.action_item_id)}" data-option-id="${esc(o.option_id)}">${[1,2,3,4,5].map(n=>`<option value="${n}">${n} 分</option>`).join('')}</select></div>`).join('')}<div class="muted">提交后锁定，不允许修改；所有指定投票人完成后才解锁定稿提交。</div><div class="actions"><button onclick="submitVote('${t.action_item_id}')">提交评分</button></div></details>`}return `<div class="notice"><b>复合协作：多人收集 → Agent 整理 → 投票 → 定稿</b><div class="meta"><span>上游验收 ${deps.filter(x=>x.satisfied&&!x.stale).length}/${deps.length}</span><span>投票 ${votes?votes.submitted_vote_count:0}/${votes?votes.required_vote_count:(p.contributions||[]).filter(x=>x.contribution_type==='VOTE').length}</span><span>最终提交：${p.final_submission_ready?'已解锁':'未解锁'}</span></div>${waiting.length?`<div>等待会议负责人验收：${waiting.map(x=>esc(x.upstream_title)).join('、')}</div>`:''}${votes?.complete?`<div>当前入选：${votes.selected_options.map(x=>esc(x.text)).join('、')}</div>`:''}</div>${upstreamSyncPanel(t)}${ownerControls}${voteControls}`}
function upstreamSyncPanel(t){const rows=(t.collaboration_inputs?.upstream_results)||[];if(!rows.length)return '';const full=rows.some(r=>r.detail_level==='FULL');return `<details ${full?'open':''}><summary>上游成果同步（${rows.length}）${full?'':' · 仅摘要'}</summary><div class="muted">${full?'你是本次定稿的最终负责人，可以读到每位同事已验收成果的正文与附件文本，用于整理候选。':'同事提交的正文只对最终负责人开放；这里同步谁交付了什么方向与提交简介。'}</div><div class="activity-list">${rows.map(r=>{const texts=(r.attachment_texts||[]).filter(x=>x.extracted_text);return `<div class="activity-item"><div class="row between"><b>${esc(r.submitted_by_display_name||'未知提交人')} · ${esc(r.title||'')}</b><span class="pill ok">已验收</span></div>${r.responsibility?`<div class="muted">负责方向：${esc(r.responsibility)}</div>`:''}${r.submission_summary?`<div>提交简介：${esc(r.submission_summary)}</div>`:''}${r.completion_report?`<div class="muted">验收报告：${esc(r.completion_report)}</div>`:''}${r.submitted_content?`<div class="quote">${esc(r.submitted_content)}</div>`:''}${texts.length?`<div class="muted">附件正文：${texts.map(x=>esc(x.name||'附件')).join('、')}</div>${texts.map(x=>`<div class="quote">${esc(x.extracted_text)}</div>`).join('')}`:''}</div>`}).join('')}</div></details>`}
async function prepareBallotDraft(id){try{const result=await post(`/api/action-items/${id}/ballot-draft`,{message_id:uid()});flash(`已生成 ${result.options?.length||0} 个候选；请由最终负责人确认后发布`);await load(true)}catch(e){flash(e.message,true)}}
async function openBallot(id){const includes=[...document.querySelectorAll(`[data-ballot-include="${id}"]:checked`)],options=includes.map(box=>{const option_id=box.dataset.optionId,text=document.querySelector(`[data-ballot-text="${id}"][data-option-id="${option_id}"]`)?.value?.trim()||'',source_refs=JSON.parse(decodeURIComponent(box.dataset.sourceRefs||'%5B%5D'));return {option_id,text,source_action_item_id:box.dataset.sourceActionId,source_refs}});if(options.length<2||options.some(x=>!x.text)){flash('请至少保留两个有内容的候选问题',true);return}try{await post(`/api/action-items/${id}/ballot`,{options,message_id:uid()});flash('候选问题已发布并锁定，等待指定参会者评分');await load(true)}catch(e){flash(e.message,true)}}
async function submitVote(id){const scores={};document.querySelectorAll(`[data-vote-task="${id}"]`).forEach(el=>scores[el.dataset.optionId]=Number(el.value));try{await post(`/api/action-items/${id}/vote`,{scores,message_id:uid()});flash('评分已保存；所有人完成后最终负责人可提交定稿');await load(true)}catch(e){flash(e.message,true)}}
function evidence(t){const m=t.proposal_metadata||{},hint=[m.suggested_owner_name?`提取到的人名：${esc(m.suggested_owner_name)}`:'',m.suggested_deadline_text?`提取到的时间：${esc(m.suggested_deadline_text)}`:''].filter(Boolean).join(' · '),collaboration=(m.collaborator_names||[]).length?`<div class="notice"><b>会议明确的默认协作者：</b>${esc(m.collaborator_names.join('、'))}</div>`:'<div class="muted">协作方式：单人任务（会议原文未明确指定协作者）</div>';return `<details><summary>查看会议依据</summary><div class="quote">${esc(m.source_timestamp)} · ${esc(m.source_quote)}</div>${collaboration}${hint?`<div class="muted">${hint}（仅供负责人复核，不会自动分配）</div>`:''}</details>`}
function taskHeader(t){const m=t.proposal_metadata||{};const risk=t.schedule_status==='CONFLICT'||t.schedule_status==='OVERDUE'?`<span class="pill warn">${esc(t.schedule_risk_reason)}</span>`:'';const active=t.collaborators||[],history=t.historical_collaborators||[],collaborators=active.length?`<span>当前协作者：${esc(active.map(x=>x.display_name).join('、'))}</span>`:history.length?`<span>协作历史：${esc(history.map(x=>x.display_name).join('、'))}</span>`:'<span>协作方式：单人任务</span>';const assignedRole=t.my_assignment?.assignment_role;const myRole=t.is_collaborator&&!t.is_mine?'<span class="pill ok">我以协作者参与</span>':t.has_collaborated&&!t.is_mine?'<span class="pill">我参与过协作</span>':t.is_mine?'<span class="pill">我是主负责人</span>':assignedRole==='OWNER'?'<span class="pill">派发角色：主负责人</span>':assignedRole==='COLLABORATOR'?'<span class="pill ok">派发角色：协作者</span>':'';const assignmentSummary=(t.current_assignments||[]).length?`<div class="meta"><span>派发版本 v${esc(t.definition_version)}</span>${t.current_assignments.map(a=>`<span>${esc(a.display_name)} · ${a.assignment_role==='OWNER'?'主负责人':'协作者'} · ${esc(activityStatusLabel(a.response_status))}</span>`).join('')}</div>`:'';return `<div class="row between"><h3>${esc(t.title)}</h3><div class="row">${myRole}<span class="pill">${esc(phase(t))}</span></div></div><div class="meta"><span>${esc(m.priority||'P1')}</span><span>主负责人：${esc(t.owner_display_name||t.assigned_owner_display_name||'待派发')}</span>${collaborators}<span>团队需要：${esc(localTime(t.team_required_by_sim_time))}</span>${t.promised_by_sim_time?`<span>我的承诺：${esc(localTime(t.promised_by_sim_time))}</span>`:''}${risk}</div>${assignmentSummary}<div><b>交付物：</b>${esc(m.deliverable||'待负责人补充')}</div>${m.work_requirements?`<div><b>工作要求：</b>${esc(m.work_requirements)}</div>`:''}${m.management_review_policy?`<div class="notice"><b>管理侧验收规则：</b>${esc(m.management_review_policy)}</div>`:''}${evidence(t)}`}
const activityStatusLabel=status=>({ACTIVE:'当前承诺',SUPERSEDED:'已失效',PENDING:'待回应',RETURNED:'已退回重改',APPROVED:'已批准',EXECUTED:'已批准并生效',REJECTED:'已驳回',FAILED:'失败',VALIDATION_FAILED:'校验未通过',PENDING_ACCEPTANCE:'待验收',ACCEPTED:'已接受',DELIVERED:'已送达',PLANNED:'待发送',BLOCKED:'阻塞',UPDATED:'已更新',PASSED:'校验通过',ON_TRACK:'按计划',AT_RISK:'有风险',WAITING_INPUT:'等待输入',READY_TO_SUBMIT:'准备提交',OPEN:'待接手',ACKNOWLEDGED:'已接手',RESOLVED:'已完成',CANCELLED:'已取消',CONTRIBUTION_RECEIVED:'待负责人处理',AWAITING_OWNER:'待负责人处理',INCLUDED:'已纳入资料',REVISION_REQUESTED:'需补充',PROMOTED:'已送入验收'}[status]||status);
const activityStatusClass=status=>['REJECTED','FAILED','VALIDATION_FAILED','BLOCKED','AT_RISK','CANCELLED','REVISION_REQUESTED'].includes(status)?'bad':['PENDING','PENDING_ACCEPTANCE','PLANNED','WAITING_INPUT','OPEN','CONTRIBUTION_RECEIVED','AWAITING_OWNER'].includes(status)?'warn':['ACTIVE','APPROVED','EXECUTED','ACCEPTED','DELIVERED','UPDATED','PASSED','ON_TRACK','READY_TO_SUBMIT','ACKNOWLEDGED','RESOLVED','INCLUDED','PROMOTED'].includes(status)?'ok':'';
function localTime(value){if(!value)return '—';const date=new Date(value);return Number.isNaN(date.getTime())?String(value||'—'):date.toLocaleString('zh-CN',{hour12:false})}
function activityPanel(t){const items=t.activity||[],result=acceptedResult(t);if(!items.length)return result+'<div class="muted">还没有协作记录</div>';return result+`<details><summary>协作记录（${items.length}）</summary><div class="activity-list">${items.map(item=>`<div class="activity-item"><div class="row between"><b>${esc(item.title)}</b><span class="pill ${activityStatusClass(item.status)}">${esc(activityStatusLabel(item.status))}</span></div><div class="activity-detail">${esc(item.detail)}</div><div class="meta"><span>${esc(item.actor||'系统')}</span><span>${esc(localTime(item.sim_time))}</span></div></div>`).join('')}</div></details>`}
function acceptedResult(t){const v=t.current_version,p=v?.payload||{},r=t.accepted_task_result,report=r?.collaboration_report;if(!v)return '';const files=(p.files||[]).map(file=>esc(file.name||'附件')).join('、');return `<details><summary>已验收结果</summary>${r?.completion_report?`<div class="quote"><b>完成报告</b><div>${esc(r.completion_report)}</div></div>`:`<div class="quote"><b>${esc(p.summary||'已完成交付')}</b>${p.content?`<div>${esc(p.content)}</div>`:''}</div>`}${p.links?.length?`<div><b>来源链接：</b>${p.links.map(link=>`<div>${esc(link)}</div>`).join('')}</div>`:''}${files?`<div><b>附件：</b>${files}</div>`:''}<div class="meta"><span>有效版本：${esc(v.version_id)}</span>${r?`<span>冻结结果：${esc(r.accepted_task_result_id)}</span><span>协作报告：${esc(r.collaboration_report_status)}</span>`:''}<span>验收意见：${esc(v.review_comment||'无补充意见')}</span></div>${report?`<details><summary>任务协作事实报告</summary><div class="meta"><span>承诺修订 ${esc(report.commitment_revisions?.length||0)}</span><span>业务信号 ${esc(report.signals?.length||0)}</span><span>求助 ${esc(report.assistance?.length||0)}</span><span>交付版本 ${esc(report.delivery_versions?.length||0)}</span></div><div class="muted">来源事件 ${esc(report.source_event_ids?.length||0)} 条；报告绑定 ${esc(report.accepted_version_id)}</div></details>`:''}</details>`}
function citedList(title,items){if(!items?.length)return '';return `<div><b>${esc(title)}</b><ul>${items.map(item=>`<li>${esc(item.text)} <span class="muted">来源：${esc((item.source_version_ids||[]).join('、'))}</span></li>`).join('')}</ul></div>`}
function organizedFinal(payload){const report=payload?.organized_report;if(!report)return `<details><summary>查看原始终稿数据</summary><pre>${esc(JSON.stringify(payload,null,2))}</pre></details>`;const processing=payload.processing||{};const mode=processing.mode==='bailian'?`百炼 ${processing.model||''}`:processing.mode==='deterministic_fallback'?'本地模板（模型不可用时回退）':'本地确定性模板';return `<div class="quote"><h3>${esc(report.title)}</h3><div>${esc(report.executive_summary)}</div></div>${citedList('关键发现',report.key_findings)}${(report.sections||[]).map(section=>`<div class="activity-item"><h3>${esc(section.heading)}</h3><div><b>结论：</b>${esc(section.summary)}</div>${section.detail?`<div class="activity-detail">${esc(section.detail)}</div>`:''}${section.links?.length?`<div class="meta"><span>链接：${section.links.map(esc).join('、')}</span></div>`:''}<div class="meta"><span>来源版本：${esc(section.source_version_id)}</span><span>冻结结果：${esc(section.accepted_task_result_id)}</span></div></div>`).join('')}${citedList('风险与缺口',report.risks_or_gaps)}${citedList('建议下一步',report.recommended_next_steps)}<div class="meta"><span>整理方式：${esc(mode)}</span>${processing.fallback_reason?`<span>回退原因：${esc(processing.fallback_reason)}</span>`:''}</div><details><summary>查看原始交付与处理数据</summary><pre>${esc(JSON.stringify(payload,null,2))}</pre></details>`}
function processingStatus(d){const p=d.result_processing||{},job=p.job;if(!p.automatic)return '<div class="notice error">部署策略已关闭自动结果整理。</div>';const mode=p.mode==='bailian'?'百炼受约束 Prompt':'本地确定性模板';if(!job)return `<div class="notice">自动整理已启用：${esc(mode)}；系统会在全部必需任务验收后主动执行。</div>`;const labels={PENDING:'等待处理',CLAIMED:'处理中',RETRY_WAIT:'等待自动重试',DELIVERED:'处理完成',DEAD_LETTER:'重试耗尽'};const latest=job.latest_event?.payload||{};return `<div class="notice ${job.status==='DEAD_LETTER'?'error':''}"><b>自动整理：${esc(labels[job.status]||job.status)}</b><div class="meta"><span>方式：${esc(mode)}</span><span>尝试：${esc(job.attempt_count)}</span>${latest.error?`<span>原因：${esc(latest.error)}</span>`:''}</div></div>`}
function finalCard(d,canAggregate){const status=processingStatus(d);if(canAggregate)return `${status}<div>全部必需交付均已验收，系统正在主动读取正文与附件并整理终稿，无需人工触发。</div>`;if(!d.final)return `${status}<div class="empty">全部必需交付验收通过后将自动整理终稿</div>`;const review=d.final.release_review;const rejected=review?.status==='REJECTED'?`<div class="notice error"><b>终稿未发布</b><div>${esc(review.comment)}</div><div class="muted">请让对应执行人提交新版本并重新验收；系统会自动废止本稿并生成下一修订。</div></div>`:'';return `${status}${rejected}<div class="row between"><b>终稿修订 ${esc(d.final.revision_no)}</b><span class="pill">${esc(d.final.status)}</span></div><div class="meta"><span>${d.lineage.length} 条原始字段来源记录</span></div>${organizedFinal(d.final.payload||{})}`}
function toggleAssignmentPopover(){const box=document.querySelector('#assignment-popover');if(!box)return;box.classList.toggle('hidden');if(!box.classList.contains('hidden')){markNoticesSeen();renderAssignmentBell(state);document.querySelector('#assignment-popover')?.classList.remove('hidden')}}
// Clicking away closes it. Without this the panel covers the task list and the
// only way out is the bell, which is not where anyone looks first.
document.addEventListener('click',e=>{const panel=document.querySelector('#assignment-popover');if(!panel||panel.classList.contains('hidden'))return;if(panel.contains(e.target)||e.target.closest('.bell'))return;panel.classList.add('hidden')});
document.addEventListener('keydown',e=>{if(e.key!=='Escape')return;document.querySelector('#assignment-popover')?.classList.add('hidden')});
function toggleOtherReason(id){const picked=String(field(`assignment-${id}-reason`)||'');const box=document.querySelector(`#other-reason-${id}`);if(box)box.classList.toggle('hidden',picked!==OTHER_REASON)}
function returnReasonFor(id){const picked=String(field(`assignment-${id}-reason`)||'').trim();if(picked===OTHER_REASON)return String(field(`assignment-${id}-other`)||'').trim();return picked}
async function respondAssignment(id,decision){const note=String(field(`assignment-${id}-message`)||'').trim();const response_message=decision==='RETURN_FOR_REVISION'?returnReasonFor(id):note;if(decision==='RETURN_FOR_REVISION'&&!response_message){const picked=String(field(`assignment-${id}-reason`)||'');flash(picked===OTHER_REASON?'选择“其他”时请填写退回原因':'退回重改时请先选择退回原因',true);return}try{await post(`/api/action-items/${id}/assignment-response`,{decision,response_message,message_id:uid()});clearDraftPrefix(`assignment-${id}`);flash(decision==='ACCEPT'?'已接受派发；全部成员接受后任务开始执行':'已退回负责人修改；本轮其他响应已经失效');await load(true)}catch(e){flash(e.message,true)}}
// Notices that ask nothing still have to be seen once. There is no read model
// in the domain for that -- and inventing one would put a UI concern into the
// audit trail -- so "seen" lives in this browser: opening the popover records
// the newest notice, and anything after it is what the badge counts.
function seenNoticeKey(){return `seenNotice:${state?.principal?.actor_id||''}`}
function unseenNotices(d){const all=(d.notices||[]).filter(n=>!n.decides);const seen=localStorage.getItem(seenNoticeKey())||'';if(!seen)return all;const at=all.findIndex(n=>n.notice_id===seen);return at<0?all:all.slice(0,at)}
function markNoticesSeen(){const newest=(state?.notices||[]).filter(n=>!n.decides)[0];if(newest)localStorage.setItem(seenNoticeKey(),newest.notice_id)}
function renderAssignmentBell(d){const host=document.querySelector('#assignment-notice');if(!host)return;const pending=(d.tasks||[]).filter(t=>t.status==='PENDING_ASSIGNMENT'&&t.my_assignment?.response_status==='PENDING');const votes=(d.tasks||[]).filter(awaitsMyVote),ballots=(d.tasks||[]).filter(awaitsMyBallot);const notices=unseenNotices(d);const total=pending.length+votes.length+ballots.length+notices.length;if(route!=='tasks'){host.innerHTML='';return}host.innerHTML=`<button class="bell" aria-label="待我响应的事项" onclick="toggleAssignmentPopover()">🔔${total?`<span class="bell-count">${total}</span>`:''}</button><div id="assignment-popover" class="assignment-popover hidden"><div class="row between"><b>待我响应</b><span class="pill">${total} 项</span></div>${pending.map(t=>`<article class="card"><div class="row between"><b>${esc(t.title)}</b><span class="pill">${t.my_assignment.assignment_role==='OWNER'?'主负责人':'协作者'}</span></div><div class="muted">${esc(t.my_assignment.assignment_message||'负责人未补充派发留言')}</div><div class="meta"><span>团队需要：${esc(localTime(t.team_required_by_sim_time))}</span><span>任务版本：v${esc(t.definition_version)}</span></div><label>回应留言（接受时可不填）</label><textarea data-draft="assignment-${t.action_item_id}-message">${esc(draft(`assignment-${t.action_item_id}-message`))}</textarea><label>退回原因（退回时必选）</label><select data-draft="assignment-${t.action_item_id}-reason" onchange="toggleOtherReason('${t.action_item_id}')"><option value="">请选择退回原因</option>${RETURN_REASONS.concat([OTHER_REASON]).map(r=>`<option value="${esc(r)}"${draft(`assignment-${t.action_item_id}-reason`)===r?' selected':''}>${esc(r)}</option>`).join('')}</select><div id="other-reason-${t.action_item_id}" class="${draft(`assignment-${t.action_item_id}-reason`)===OTHER_REASON?'':'hidden'}"><label>请填写退回原因</label><textarea data-draft="assignment-${t.action_item_id}-other">${esc(draft(`assignment-${t.action_item_id}-other`))}</textarea></div><div class="actions"><button class="good" onclick="respondAssignment('${t.action_item_id}','ACCEPT')">接受</button><button class="danger" onclick="respondAssignment('${t.action_item_id}','RETURN_FOR_REVISION')">退回重改</button></div></article>`).join('')}${ballots.map(t=>`<article class="card"><div class="row between"><b>${esc(t.title)}</b><span class="pill warn">待整理候选</span></div><div class="muted">上游问题清单已全部验收，可以生成候选并发起投票。</div><div class="actions"><button onclick="focusTask('${t.action_item_id}')">去整理</button></div></article>`).join('')}${votes.map(t=>`<article class="card"><div class="row between"><b>${esc(t.title)}</b><span class="pill warn">待我打分</span></div><div class="muted">候选问题已发布，所有指定投票人完成后才解锁定稿提交。</div><div class="actions"><button onclick="focusTask('${t.action_item_id}')">去打分</button></div></article>`).join('')}${notices.map(n=>`<article class="card"><div class="row between"><b>${esc(n.title)}</b><span class="pill">通知</span></div><div class="muted">${esc(n.summary)}</div>${(n.fields||[]).map(f=>`<div class="meta"><span>${esc(f.label)}：${esc(f.value)}</span></div>`).join('')}<div class="actions"><button onclick="focusTask('${n.action_item_id}')">去看任务</button></div></article>`).join('')}${total?'':'<div class="empty">当前没有需要回应的事项</div>'}</div>`}
function focusTask(id){selectedTaskId=id;toggleAssignmentPopover();render(state);document.querySelector('.task-focus')?.scrollIntoView({behavior:'smooth',block:'start'})}
async function revisePersonalCommitment(id){const local=field(`deadline-${id}-value`),reason=field(`deadline-${id}-reason`);if(!local){flash('请填写新的个人承诺时间',true);return}try{await post(`/api/action-items/${id}/personal-commitment`,{proposed_deadline_sim_time:new Date(local).toISOString(),reason,message_id:uid()});clearDraftPrefix(`deadline-${id}`);flash('个人承诺已更新；团队需要时间未改变');await load(true)}catch(e){flash(e.message,true)}}
const fileSizeLabel=bytes=>bytes>=1024*1024?`${(bytes/1024/1024).toFixed(1)}MB`:`${Math.max(1,Math.round(bytes/1024))}KB`;
function fileListMarkup(id){const files=pendingFiles[id]||[];if(!files.length)return '<div class="muted">尚未选择附件</div>';const total=files.reduce((sum,f)=>sum+f.size,0);return `<div class="activity-list">${files.map((f,i)=>`<div class="row between"><span>${esc(f.name)} · ${esc(fileSizeLabel(f.size))}</span><button class="danger" type="button" onclick="removeSelectedFile('${id}',${i})">移除</button></div>`).join('')}</div><div class="muted">共 ${files.length}/${MAX_ATTACHMENT_COUNT} 个，合计 ${esc(fileSizeLabel(total))}/${ATTACHMENT_LIMIT_MB}MB</div>`}
function renderFileList(id){const host=document.querySelector(`#filelist-${id}`);if(host)host.innerHTML=fileListMarkup(id)}
async function addSelectedFiles(id){const input=document.querySelector(`#files-${id}`);if(!input)return;const existing=pendingFiles[id]||[];const added=[];for(const file of [...input.files]){if(existing.some(f=>f.name===file.name&&f.size===file.size))continue;if(existing.length+added.length>=MAX_ATTACHMENT_COUNT){flash(`最多只能上传 ${MAX_ATTACHMENT_COUNT} 个附件`,true);break}if(file.size>ATTACHMENT_LIMIT_BYTES){flash(`${file.name} 超过单文件 ${ATTACHMENT_LIMIT_MB}MB 上限`,true);continue}const total=[...existing,...added].reduce((sum,f)=>sum+f.size,0);if(total+file.size>ATTACHMENT_LIMIT_BYTES){flash(`附件总大小不能超过 ${ATTACHMENT_LIMIT_MB}MB`,true);break}const data=await new Promise((resolve,reject)=>{const reader=new FileReader();reader.onload=()=>resolve(reader.result);reader.onerror=reject;reader.readAsDataURL(file)});added.push({name:file.name,size:file.size,type:file.type,data})}pendingFiles[id]=[...existing,...added];input.value='';renderFileList(id)}
function removeSelectedFile(id,index){const files=pendingFiles[id]||[];files.splice(index,1);pendingFiles[id]=files;renderFileList(id)}
const validationLabel=key=>({summary:'结果摘要',content_or_link_or_file:'正文、链接或附件至少一项',readable_attachment_or_content:'附件必须可读取；请粘贴正文或上传 PDF/TXT/DOCX/XLSX/PPTX'}[key]||key);
function validationNotice(t){if(t.latest_version?.validation_status!=='FAILED')return '';const missing=t.latest_version.validation_errors?.missing_fields||[];return `<div class="notice error"><b>最近一次提交未通过校验：</b>${esc(missing.map(validationLabel).join('、')||'交付内容不符合要求')}。请修改后重新提交，失败版本已经保留。</div>`}
async function submitDelivery(id){const summary=String(field(`delivery-${id}-summary`)||'').trim();const content=String(field(`delivery-${id}-content`)||'').trim();const linkText=String(field(`delivery-${id}-links`)||'');const links=linkText.split('\n').map(s=>s.trim()).filter(Boolean);const files=pendingFiles[id]||[];const missing=[];if(!summary)missing.push('结果摘要');if(!content&&!links.length&&!files.length)missing.push('正文、链接或附件至少一项');if(missing.length){flash('提交前请补充：'+missing.join('、'),true);document.querySelector(`[data-draft="delivery-${id}-${!summary?'summary':'content'}"]`)?.focus();return}try{const result=await post(`/api/action-items/${id}/submit`,{message_id:uid(),delivery:{summary,content,links,files,completion_note:String(field(`delivery-${id}-note`)||'').trim()}});if(result.validation_status==='FAILED'){flash('校验未通过：'+(result.missing_fields||[]).map(validationLabel).join('、')+'；填写内容已保留',true);await load(true);return}clearDraftPrefix(`delivery-${id}`);delete pendingFiles[id];flash(result.submission_kind==='CONTRIBUTION'?'协作成果已提交，等待任务负责人处理；整项任务仍在进行中':'任务成果已提交，等待会议负责人验收');await load(true)}catch(e){flash(e.message,true)}}
async function sendSignal(id,signalType){try{await post(`/api/action-items/${id}/signal`,{signal_type:signalType,note:String(field(`signal-${id}-note`)||'').trim(),message_id:uid()});clearDraftPrefix(`signal-${id}`);flash('任务状态已记录');await load(true)}catch(e){flash(e.message,true)}}
async function requestHelp(id){const target=String(field(`help-${id}-target`)||''),summary=String(field(`help-${id}-summary`)||'').trim();if(!target||!summary){flash('请选择求助对象并说明需要什么帮助',true);return}try{await post(`/api/action-items/${id}/assistance`,{target_actor_id:target,category:field(`help-${id}-category`)||'OTHER',summary,message_id:uid()});clearDraftPrefix(`help-${id}`);flash('求助已发送给本次参会者');await load(true)}catch(e){flash(e.message,true)}}
async function updateHelp(id,action){let resolution_summary='';if(action==='resolve'){resolution_summary=String(prompt('请简要说明如何解决')||'').trim();if(!resolution_summary)return}try{await post(`/api/assistance/${id}/${action}`,{resolution_summary,message_id:uid()});flash(action==='acknowledge'?'已确认接手':action==='resolve'?'求助已解决':'求助已取消');await load(true)}catch(e){flash(e.message,true)}}
function assistancePanel(t){const h=t.active_assistance;if(!h)return '';const me=state?.principal?.actor_id;const actions=[];if(me===h.target_actor_id&&h.status==='OPEN')actions.push(`<button onclick="updateHelp('${h.assistance_request_id}','acknowledge')">确认接手</button>`);if((me===h.target_actor_id||me===h.requester_actor_id)&&['OPEN','ACKNOWLEDGED'].includes(h.status))actions.push(`<button class="good" onclick="updateHelp('${h.assistance_request_id}','resolve')">标记解决</button>`);if(me===h.requester_actor_id&&['OPEN','ACKNOWLEDGED'].includes(h.status))actions.push(`<button class="danger" onclick="updateHelp('${h.assistance_request_id}','cancel')">取消求助</button>`);return `<div class="notice"><b>协作求助 · ${esc(h.status)}</b><div>${esc(h.summary||h.category)}</div><div class="meta"><span>求助给：${esc(h.target_display_name)}</span><span>${esc(localTime(h.created_sim_time))}</span></div>${actions.length?`<div class="actions">${actions.join('')}</div>`:''}</div>`}
function signalAndHelpControls(t){if(t.status!=='TRACKING')return '';const me=state?.principal?.actor_id;const options=(state?.participants||[]).filter(p=>p.actor_id!==me).map(p=>`<option value="${esc(p.actor_id)}">${esc(p.display_name)}</option>`).join('');const signalLabels={ON_TRACK:'按计划',AT_RISK:'有风险',BLOCKED:'被阻塞',WAITING_INPUT:'等待输入',READY_TO_SUBMIT:'准备提交'};const signals=Object.entries(signalLabels).map(([value,label])=>`<button class="secondary" onclick="sendSignal('${t.action_item_id}','${value}')">${label}</button>`).join('');const help=t.active_assistance?'':`<details><summary>向参会者求助</summary><div class="form-grid"><div><label>求助谁</label><select data-draft="help-${t.action_item_id}-target"><option value="">选择本次参会者</option>${options}</select></div><div><label>类型</label><select data-draft="help-${t.action_item_id}-category"><option value="CAPACITY">忙不过来</option><option value="EXPERTISE">需要专业协作</option><option value="DEPENDENCY">等待依赖</option><option value="DECISION">需要决定</option><option value="OTHER">其他</option></select></div><div class="wide"><label>需要什么帮助</label><textarea data-draft="help-${t.action_item_id}-summary">${esc(draft(`help-${t.action_item_id}-summary`))}</textarea></div></div><div class="actions"><button onclick="requestHelp('${t.action_item_id}')">发送求助</button></div></details>`;return `<details open><summary>快捷状态</summary><div class="muted">点击即记录有效业务信号；浏览和刷新页面不会算进展。</div><label>补充说明（可选）</label><input data-draft="signal-${t.action_item_id}-note" value="${esc(draft(`signal-${t.action_item_id}-note`))}"><div class="actions">${signals}</div></details>${help}`}
function deliveryControls(t){const failed=t.latest_version?.validation_status==='FAILED'?(t.latest_version.payload||{}):{},isContribution=Boolean(t.is_collaborator&&!t.is_mine),title=isContribution?'提交协作成果':t.status==='TRACKING'?'提交任务成果':'提交新版',button=isContribution?'提交协作成果':t.status==='TRACKING'?'提交并申请验收':'提交新版并申请验收';return `<details><summary>${title}</summary><div class="muted">提交条件：结果摘要必填；正文、链接或附件至少提供一项。${isContribution?' 协作提交不会结束整项任务，由任务负责人决定如何采用。':''}</div><div class="form-grid"><div class="wide"><label>结果摘要（必填）</label><textarea required data-draft="delivery-${t.action_item_id}-summary">${esc(draft(`delivery-${t.action_item_id}-summary`,failed.summary||''))}</textarea></div><div class="wide"><label>正文</label><textarea data-draft="delivery-${t.action_item_id}-content">${esc(draft(`delivery-${t.action_item_id}-content`,failed.content||''))}</textarea></div><div><label>链接（每行一个）</label><textarea data-draft="delivery-${t.action_item_id}-links">${esc(draft(`delivery-${t.action_item_id}-links`,failed.links?.join('\n')||''))}</textarea></div><div><label>附件（最多 ${MAX_ATTACHMENT_COUNT} 个，单个与总计均不超过 ${ATTACHMENT_LIMIT_MB}MB）</label><input id="files-${t.action_item_id}" type="file" multiple onchange="addSelectedFiles('${t.action_item_id}')"><div id="filelist-${t.action_item_id}">${fileListMarkup(t.action_item_id)}</div></div><div class="wide"><label>完成说明</label><textarea data-draft="delivery-${t.action_item_id}-note">${esc(draft(`delivery-${t.action_item_id}-note`,failed.completion_note||''))}</textarea></div></div><div class="actions"><button class="good" onclick="submitDelivery('${t.action_item_id}')">${button}</button></div></details>`}
function taskProcessingReceipt(t){const v=t.latest_version;if(!v||v.validation_status==='FAILED')return '';if(v.is_contribution){if(['PENDING','PROCESSING','RETRY_WAIT'].includes(v.processing_status))return '<div class="notice">协作成果已接收，AI 正在分析它覆盖了任务的哪些部分；整项任务仍在进行中。</div>';if(v.processing_status==='FAILED')return `<div class="notice error">协作成果分析失败：${esc(v.processing_error_code)}。原始贡献已保留，任务负责人仍可处理。</div>`;if(v.processing_status==='READY')return '<div class="notice">协作成果分析已完成，等待任务负责人纳入、要求补充或送入最终验收。</div>';return ''}if(['PENDING','PROCESSING','RETRY_WAIT'].includes(v.processing_status))return '<div class="notice">交付已接收，正在自动生成验收辅助包。</div>';if(v.processing_status==='FAILED')return `<div class="notice error">自动处理失败：${esc(v.processing_error_code)} · ${esc(v.processing_error_stage)}。原始提交已保留，负责人可人工查看。</div>`;if(v.processing_status==='READY')return '<div class="notice">验收辅助包已生成，等待会议负责人验收。</div>';return ''}
async function decideContribution(versionId,action){const comment=String(field(`contribution-${versionId}-comment`)||'').trim();if(action==='REQUEST_REVISION'&&!comment){flash('请先填写需要协作者补充的内容',true);return}try{const result=await post(`/api/artifact-versions/${versionId}/contribution`,{action,comment,message_id:uid()});clearDraftPrefix(`contribution-${versionId}`);const labels={INCLUDED:'协作成果已纳入任务资料，任务继续推进',REVISION_REQUESTED:'补充要求已进入协作记录',PROMOTED:'该贡献已作为最终候选送入任务验收'};flash(labels[result.contribution_status]||'协作成果状态已更新');await load(true)}catch(e){flash(e.message,true)}}
function contributionAnalysis(v){if(['PENDING','PROCESSING','RETRY_WAIT'].includes(v.processing_status))return '<div class="muted">AI 正在分析该贡献覆盖了完整任务的哪些部分。</div>';if(v.processing_status==='FAILED')return `<div class="notice error">贡献分析失败：${esc(v.processing_error_code)}；不影响负责人查看原始内容。</div>`;const result=v.processing_result||{},alignment=result.task_alignment||{},advice=result.acceptance_advice||{};if(v.processing_status!=='READY')return '';return `<div class="notice"><b>贡献覆盖：${esc(alignment.status)}</b><div>${esc(alignment.reason)}</div><div class="meta"><span>若直接送验：${esc(advice.decision||'无建议')}</span></div></div>`}
function contributionPanel(t){const versions=t.contribution_versions||[];if(!versions.length)return '';return `<details open><summary>协作成果处理（${versions.length}）</summary><div class="activity-list">${versions.map(v=>{const p=v.payload||{},files=(p.files||[]).map(x=>x.name).filter(Boolean).join('、'),canHandle=t.is_mine&&v.validation_status==='PASSED'&&v.review_status==='NOT_REQUIRED'&&v.contribution_status==='AWAITING_OWNER';return `<div class="activity-item"><div class="row between"><b>${esc(v.submitted_by_display_name)} · 贡献版本 ${esc(v.received_sequence)}</b><span class="pill ${activityStatusClass(v.contribution_status)}">${esc(activityStatusLabel(v.contribution_status))}</span></div><div>${esc(p.summary||'未填写摘要')}</div>${p.content?`<div class="quote">${esc(p.content)}</div>`:''}${files?`<div class="muted">附件：${esc(files)}</div>`:''}${contributionAnalysis(v)}${v.decision?.comment?`<div class="notice"><b>负责人反馈：</b>${esc(v.decision.comment)}</div>`:''}${canHandle?`<label>处理说明／补充要求</label><textarea data-draft="contribution-${v.version_id}-comment">${esc(draft(`contribution-${v.version_id}-comment`))}</textarea><div class="actions"><button class="secondary" onclick="decideContribution('${v.version_id}','INCLUDE')">纳入任务资料</button>${v.can_request_revision?`<button class="danger" onclick="decideContribution('${v.version_id}','REQUEST_REVISION')">要求协作者补充</button>`:'<span class="muted">协作已结束；如需补充请重新邀请</span>'}<button class="good" onclick="decideContribution('${v.version_id}','PROMOTE')">作为最终候选送验</button></div>`:''}</div>`}).join('')}</div></details>`}
function taskCollaborationCard(t){const mine=Boolean(t.is_mine),collaborator=Boolean(t.is_collaborator),canContribute=Boolean(t.can_contribute);let controls='';if(t.status==='PENDING_ASSIGNMENT'&&t.my_assignment?.response_status==='ACCEPTED'){controls='<div class="notice">你已接受当前任务版本，正在等待其他成员回应；全部接受后才会进入执行。</div>'}else if(t.status==='NEEDS_REVISION'){controls='<div class="notice error">本轮派发已退回负责人修改。新版本重新派发前，任务暂停执行和提交。</div>'}else if(canContribute&&['TRACKING','ACCEPTED','AGGREGATED'].includes(t.status)){const commitment=mine?`<details><summary>修改个人承诺</summary><div class="form-grid"><div><label>新的个人承诺时间</label><input data-draft="deadline-${t.action_item_id}-value" type="datetime-local" value="${esc(draft(`deadline-${t.action_item_id}-value`))}"></div><div><label>修改原因（可选）</label><input data-draft="deadline-${t.action_item_id}-reason" value="${esc(draft(`deadline-${t.action_item_id}-reason`))}"></div></div><div class="actions"><button class="secondary" onclick="revisePersonalCommitment('${t.action_item_id}')">保存个人承诺</button></div></details>`:'';controls=`${collaborator?'<div class="notice"><b>协作执行空间</b><div>你提交的是协作贡献，不会直接结束整项任务；任务负责人会在同一版本链中处理。</div></div>':''}${signalAndHelpControls(t)}${commitment}${deliveryControls(t)}`}else if(canContribute&&t.status==='PENDING_ACCEPTANCE'){controls='<div class="notice">任务负责人已确认最终候选，正在等待会议负责人验收。</div>'}else if(t.has_collaborated&&!canContribute){controls='<div class="notice">本次协作已结束，你可以继续查看任务状态和自己的历史贡献；需要再次修改时由任务负责人重新邀请。</div>'}return `<article class="card task ${t.last_owner_signal?.signal_type==='BLOCKED'?'blocked':''} ${t.status==='ACCEPTED'?'done':''}">${taskHeader(t)}${t.last_owner_signal?`<div class="meta"><span>最近业务信号：${esc(t.last_owner_signal.signal_type)}</span><span>${esc(localTime(t.last_owner_signal.signal_at))}</span></div>`:''}${collaborationHintPanel(t)}${assistancePanel(t)}${taskProcessingReceipt(t)}${validationNotice(t)}${contributionPanel(t)}${collaborationPanel(t)}${activityPanel(t)}${controls}</article>`}
async function decideMemory(id,action,code){const replacement_code=action==='replace'?String(code||''):'';try{await post(`/api/memories/${id}/${action}`,{replacement_code,message_id:uid()});flash(action==='confirm'?'已确认，同事会看到这一条':action==='reject'?'已拒绝，不会再提示':'已更新为你选的词条');await load(true)}catch(e){flash(e.message,true)}}
function memoryTopicSpec(topic){return (state?.memory_lexicon?.topics||[]).find(x=>x.topic===topic)}
function memoryChoices(m){const spec=memoryTopicSpec(m.topic),observed=m.value?.code;return (spec?.values||[]).map(x=>`<label class="row"><input style="width:auto" type="radio" name="memory-${esc(m.memory_id)}" value="${esc(x.code)}" ${x.code===observed?'checked':''}><span>${esc(x.label)}${x.code===observed&&m.status==='PRIVATE_DRAFT'?' <span class="pill warn">系统观察到</span>':''}</span></label><div class="muted" style="margin-left:1.4rem">${esc(x.collaborator_hint||'')}</div>`).join('')}
function memoryBreadth(m){const n=m.value?.distinct_action_item_count;if(!n)return '';return n<2?'<span class="pill warn">仅在 1 个任务中观察到</span>':`<span class="pill">在 ${esc(n)} 个任务中观察到</span>`}
function memoryCard(m){const spec=memoryTopicSpec(m.topic),draft=m.status==='PRIVATE_DRAFT',active=['PRIVATE_DRAFT','CONFIRMED'].includes(m.status);return `<article class="card"><div class="row between"><b>${esc(spec?.title||m.topic)}</b><span class="pill ${m.status==='CONFIRMED'?'ok':draft?'warn':''}">${esc(m.status)}</span></div><div class="muted">${esc(spec?.prompt||'')}</div>${active?memoryChoices(m):`<div class="quote">${esc(m.value?.statement||'')}</div>`}<div class="meta"><span>${esc(m.origin==='SELF_DECLARED'?'我自己填写':'系统从已验收事实提出')}</span>${draft?memoryBreadth(m):''}<span>版本 ${esc(m.version)}</span></div>${active?`<div class="actions">${draft?`<button class="good" onclick="saveMemoryChoice('${m.memory_id}')">就选这条</button><button class="danger" onclick="decideMemory('${m.memory_id}','reject')">这条不像我</button>`:`<button class="secondary" onclick="saveMemoryChoice('${m.memory_id}')">更新</button>`}</div>`:''}</article>`}
async function saveMemoryChoice(id){const picked=document.querySelector(`input[name="memory-${id}"]:checked`)?.value||'';if(!picked){flash('请先选择一条',true);return}const card=(state?.memories||[]).find(x=>x.memory_id===id);const action=(card?.status==='PRIVATE_DRAFT'&&picked===card?.value?.code)?'confirm':'replace';await decideMemory(id,action,picked)}
async function declareMemory(topic){const picked=document.querySelector(`input[name="declare-${topic}"]:checked`)?.value||'';if(!picked){flash('请先选择一条',true);return}try{await post('/api/memories/declare',{topic,code:picked,message_id:uid()});flash('已更新你的协作说明书');await load(true)}catch(e){flash(e.message,true)}}
function declareCard(spec,current){return `<article class="card"><div class="row between"><b>${esc(spec.title)}</b>${current?'<span class="pill ok">已填写</span>':'<span class="pill">未填写</span>'}</div><div class="muted">${esc(spec.prompt)}</div>${spec.values.map(x=>`<label class="row"><input style="width:auto" type="radio" name="declare-${esc(spec.topic)}" value="${esc(x.code)}" ${x.code===current?'checked':''}><span>${esc(x.label)}</span></label><div class="muted" style="margin-left:1.4rem">${esc(x.collaborator_hint||'')}</div>`).join('')}<div class="actions"><button class="secondary" onclick="declareMemory('${esc(spec.topic)}')">保存</button></div></article>`}
function memorySection(d){const memories=(d.memories||[]).filter(m=>m.origin!=='SELF_DECLARED'),declared=new Map((d.memories||[]).filter(m=>m.origin==='SELF_DECLARED'&&m.status==='CONFIRMED').map(m=>[m.topic,m.value?.code]));const selfTopics=(d.memory_lexicon?.topics||[]).filter(x=>x.origin==='SELF_DECLARED');return `<section class="section"><h2>我的协作说明书</h2><div class="notice">这不是对你的评价，是「和我协作时可以注意什么」。词条固定可选，只有你能确认或修改；它不参与权限、任务状态和验收。同事只看到你当前确认的一句话，看不到证据和历史。</div>${memories.length?`<h3>系统从已验收任务里注意到的</h3><div class="muted">每组都平等列出全部选项，系统只标注它观察到哪一条；你选哪条都可以，也可以直接拒绝。</div><div class="grid two">${memories.map(memoryCard).join('')}</div>`:''}${selfTopics.length?`<h3>我希望别人怎么配合我</h3><div class="muted">这几项系统无法观察，只能由你自己填；不填也不影响任何流程。</div><div class="grid two">${selfTopics.map(spec=>declareCard(spec,declared.get(spec.topic))).join('')}</div>`:''}</section>`}
function collaborationHintPanel(t){const hints=t.collaboration_hints||[];if(!hints.length)return '';return `<details><summary>当前协作者的工作习惯提示</summary><div class="muted">只用于调整沟通和配合方式，不影响权限、任务状态或验收。</div>${hints.map(x=>`<div class="activity-item"><b>${esc(x.display_name)}</b> · ${esc(x.statement)}</div>`).join('')}</details>`}
function taskRelation(t){return t.is_mine||t.my_assignment?.assignment_role==='OWNER'?'responsible':'collaboration'}
function taskStagePercent(t){if(t.status==='PENDING_ASSIGNMENT')return 12;if(t.status==='TRACKING')return 48;if(t.status==='PENDING_ACCEPTANCE')return 78;if(['ACCEPTED','AGGREGATED','ARCHIVED'].includes(t.status))return 100;return 6}
function timelineRow(t){const relation=taskRelation(t),selected=t.action_item_id===selectedTaskId,waiting=t.status==='PENDING_ASSIGNMENT';return `<div class="timeline-row ${relation} ${selected?'selected':''}" onclick="selectTask('${t.action_item_id}')"><div><div class="row between"><b>${esc(t.title)}</b><span class="pill">${relation==='responsible'?'我负责':'我协作'}</span></div><div class="meta"><span>${esc(phase(t))}</span><span>团队需要：${esc(localTime(t.team_required_by_sim_time))}</span></div></div><div><div class="timeline-bar"><div class="timeline-fill ${waiting?'waiting':''}" style="width:${taskStagePercent(t)}%"></div></div><div class="meta"><span>${waiting?'等待全部成员接受':`阶段进度 ${taskStagePercent(t)}%`}</span></div></div></div>`}
function selectTask(id){selectedTaskId=selectedTaskId===id?null:id;renderTasks(state)}
function myParticipationInput(t,type){const me=state?.principal?.actor_id;return (t.collaboration_progress?.contributions||[]).find(x=>x.contribution_type===type&&x.actor_id===me)}
function awaitsMyVote(t){const p=t.collaboration_progress;if(!p)return false;const ballotOpen=(p.contributions||[]).some(x=>x.contribution_type==='BALLOT'&&x.status==='SUBMITTED');return ballotOpen&&myParticipationInput(t,'VOTE')?.status==='PENDING'}
function awaitsMyBallot(t){const p=t.collaboration_progress;if(!p)return false;return Boolean(p.dependencies_ready)&&myParticipationInput(t,'BALLOT')?.status==='PENDING'}
// A confirmed voter is neither owner nor collaborator on the decision task, so
// without this the card never renders and the scoring UI is unreachable.
function isMyParticipation(t){return Boolean(myParticipationInput(t,'VOTE')||myParticipationInput(t,'BALLOT'))}
function renderTasks(d){renderAssignmentBell(d);const active=d.tasks.filter(t=>t.is_mine||t.is_collaborator||isMyParticipation(t)||(t.status==='PENDING_ASSIGNMENT'&&t.my_assignment?.response_status==='ACCEPTED')),past=d.tasks.filter(t=>t.has_collaborated&&!t.is_collaborator&&!t.is_mine&&!isMyParticipation(t));if(selectedTaskId&&!active.some(t=>t.action_item_id===selectedTaskId))selectedTaskId=null;if(!selectedTaskId&&active.length)selectedTaskId=active[0].action_item_id;const selected=active.find(t=>t.action_item_id===selectedTaskId),p=d.meeting_progress||{};document.querySelector('#app').innerHTML=`<div class="top"><div><div class="eyebrow">MY EXECUTION</div><h1>我的任务执行</h1><div class="muted">负责与协作任务使用同一条时间线；协作贡献由主负责人整理后才进入会议负责人验收。</div></div><div class="meta"><span>会议任务 ${esc(p.total||0)}</span><span>执行中 ${esc(p.tracking||0)}</span><span>待验收 ${esc(p.pending_acceptance||0)}</span><span>需重改 ${esc(p.needs_revision||0)}</span></div></div><section><h2>我负责／协作中的任务</h2>${active.length?`<div class="timeline ${selectedTaskId?'has-selection':''}">${active.map(timelineRow).join('')}</div>`:'<div class="empty">当前没有进入执行的任务；新的派发会显示在右上角闹铃中。</div>'}${selected?`<div class="task-focus ${taskRelation(selected)}"><h2>当前任务与推进</h2>${taskCollaborationCard(selected)}</div>`:''}</section>${past.length?`<details class="section"><summary>我参与过的任务（${past.length}）</summary><div class="grid">${past.map(taskCollaborationCard).join('')}</div></details>`:''}${memorySection(d)}`}
function structureRevokePanel(d){const confirmed=d.tasks.filter(t=>t.proposal_metadata?.collaboration_structure&&!t.published_sim_time&&!t.owner_actor_id&&!(t.collaboration_progress?.contributions||[]).some(x=>x.status==='SUBMITTED'));if(!confirmed.length)return '';return `<div class="card"><div class="notice">已确认但尚未派发的复合协作结构。派发或开票之后就不能再撤销。</div>${confirmed.map(t=>{const s=t.proposal_metadata.collaboration_structure,names=new Map((d.participants||[]).map(p=>[p.actor_id,p.display_name]));return `<article class="activity-item"><div class="row between"><b>${esc(t.title)}</b><span class="pill warn">已确认结构</span></div><div class="muted">最终负责人：${esc(names.get(s.required_owner_actor_id)||s.required_owner_actor_id)} · 投票人 ${esc((s.voter_actor_ids||[]).length)} 人 · 保留 ${esc(s.selection_count)} 项</div><div class="muted">上游：${esc((s.collection_action_item_ids||[]).map(id=>d.tasks.find(x=>x.action_item_id===id)?.title||id).join('、'))}</div><div class="quote">${esc(s.source_span)}</div><label>撤销原因</label><input id="revoke-reason-${esc(t.action_item_id)}" placeholder="说明为什么这个结构配错了"><div class="actions"><button class="danger" onclick="revokeStructure('${esc(t.action_item_id)}')">撤销这个结构</button></div></article>`}).join('')}</div>`}
async function revokeStructure(id){const reason=String(document.querySelector(`#revoke-reason-${id}`)?.value||'').trim();if(!reason){flash('请填写撤销原因',true);return}try{const r=await post(`/api/collaboration-structures/question-vote/${id}/revoke`,{reason,message_id:uid()});flash(`结构已撤销，移除 ${r.removed_dependency_count} 条依赖和 ${r.removed_participation_input_count} 条参与记录`);await load(true)}catch(e){flash(e.message,true)}}
function structureManager(d){const candidates=d.tasks.filter(t=>t.status==='PENDING_CONFIRMATION'&&!t.owner_actor_id&&!t.published_sim_time&&!t.proposal_metadata?.collaboration_structure),participants=(d.participants||[]).filter(x=>(x.roles||[]).some(role=>['PARTICIPANT','ACTION_OWNER'].includes(role)));if(candidates.length<2||!participants.length)return '';return `<section class="section"><h2>P1 复合协作</h2><div class="card"><div class="notice">用于会议明确的“多人分别收集 → 一人汇总 → 大家打分 → 最终负责人定稿”；基础任务仍使用原状态和版本链。</div><label>最终汇总/定稿任务</label><select id="structure-manager-decision"><option value="">请选择</option>${candidates.map(x=>`<option value="${esc(x.action_item_id)}">${esc(x.title)}</option>`).join('')}</select><label>上游收集任务</label>${d.tasks.filter(x=>x.status!=='REJECTED').map(x=>`<label><input style="width:auto" type="checkbox" data-structure-manager-upstream value="${esc(x.action_item_id)}"> ${esc(x.title)}</label>`).join('')}<label>最终负责人</label><select id="structure-manager-owner"><option value="">请选择</option>${participants.map(x=>`<option value="${esc(x.actor_id)}">${esc(x.display_name)}</option>`).join('')}</select><label>投票人</label>${participants.map(x=>`<label><input style="width:auto" type="checkbox" data-structure-manager-voter value="${esc(x.actor_id)}"> ${esc(x.display_name)}</label>`).join('')}<div class="form-grid"><div><label>最终保留数量</label><input id="structure-manager-count" type="number" min="1" max="8" value="8"><div class="muted">候选必须多于保留数，否则投票选不出东西</div></div><div><label>会议原文依据</label><input id="structure-manager-source" placeholder="粘贴组织人关于收集、投票和定稿的原话"></div></div><div class="actions"><button onclick="confirmQuestionVoteFromManager()">确认协作结构</button></div></div></section>`}
async function confirmQuestionVoteFromManager(){const decision_action_item_id=document.querySelector('#structure-manager-decision')?.value||'',collection_action_item_ids=[...document.querySelectorAll('[data-structure-manager-upstream]:checked')].map(x=>x.value).filter(x=>x!==decision_action_item_id),voter_actor_ids=[...document.querySelectorAll('[data-structure-manager-voter]:checked')].map(x=>x.value),final_owner_actor_id=document.querySelector('#structure-manager-owner')?.value||'',selection_count=Number(document.querySelector('#structure-manager-count')?.value||8),source_span=document.querySelector('#structure-manager-source')?.value||'';if(!decision_action_item_id||!collection_action_item_ids.length||!voter_actor_ids.length||!final_owner_actor_id||!source_span.trim()){flash('请完整选择定稿任务、上游任务、负责人、投票人并填写会议依据',true);return}try{await post('/api/collaboration-structures/question-vote',{collection_action_item_ids,decision_action_item_id,final_owner_actor_id,voter_actor_ids,selection_count,source_span,message_id:uid()});flash('复合协作结构已确认');await load(true)}catch(e){flash(e.message,true)}}
function proposalPayload(id){const teamRequired=field(`review-${id}-team`);if(!teamRequired)throw new Error('请填写团队需要时间');return {title:field(`review-${id}-title`),deliverable:field(`review-${id}-deliverable`),work_requirements:field(`review-${id}-work`),management_review_policy:field(`review-${id}-policy`),acceptance_criteria:field(`review-${id}-policy`),team_required_by_sim_time:new Date(teamRequired).toISOString(),priority:field(`review-${id}-priority`)||'P1',message_id:uid()}}
async function saveProposal(id){try{await post(`/api/action-items/${id}/revise`,proposalPayload(id));clearDraftPrefix(`review-${id}`);flash('任务定义与团队时间已保存，会议原文保持不变');await load(true)}catch(e){flash(e.message,true)}}
async function dispatchTask(id){const owner_actor_id=String(field(`dispatch-${id}-owner`)||''),collaborator_actor_ids=[...document.querySelectorAll(`[data-dispatch-collaborator="${id}"]:checked`)].map(x=>x.value).filter(x=>x!==owner_actor_id),assignment_message=String(field(`dispatch-${id}-message`)||'').trim();if(!owner_actor_id){flash('请选择一名主负责人',true);return}try{await post(`/api/action-items/${id}/revise`,proposalPayload(id));await post(`/api/action-items/${id}/dispatch`,{owner_actor_id,collaborator_actor_ids,assignment_message,message_id:uid()});clearDraftPrefix(`review-${id}`);clearDraftPrefix(`dispatch-${id}`);flash('任务已派发；全部成员接受后进入执行');await load(true)}catch(e){flash(e.message,true)}}
async function ignoreTask(id){const reason=prompt('请输入忽略原因');if(!reason)return;try{await post(`/api/action-items/${id}/ignore`,{reason,message_id:uid()});flash('任务已忽略，记录仍保留在审计中');await load(true)}catch(e){flash(e.message,true)}}
async function mergeTask(id){const target=document.querySelector(`#merge-${id}`).value;if(!target)return;try{await post(`/api/action-items/${id}/merge`,{target_action_item_id:target,message_id:uid()});flash('任务已合并，来源依据已追加到目标任务');await load(true)}catch(e){flash(e.message,true)}}
async function reviewVersion(versionId,approve){const comment=field(`accept-${versionId}-comment`),completion_report=field(`accept-${versionId}-report`);try{await post(`/api/artifact-versions/${versionId}/review`,{approve,comment,completion_report,message_id:uid()});clearDraftPrefix(`accept-${versionId}`);flash(approve?'验收通过并冻结单任务结果':'已退回负责人修改');await load(true)}catch(e){flash(e.message,true)}}
async function retryTaskProcessing(versionId){try{await post(`/api/artifact-versions/${versionId}/retry-processing`,{message_id:uid()});flash('已重新排队，独立 Agent Worker 将再次处理');await load(true)}catch(e){flash(e.message,true)}}
function processingReview(v){if(!v)return '';if(['PENDING','PROCESSING','RETRY_WAIT'].includes(v.processing_status))return `<div class="notice"><b>验收辅助处理中</b><div class="muted">独立 Agent Worker 正在处理；当前页面不会高频轮询或覆盖尚未提交的表单。</div></div>`;if(v.processing_status==='FAILED'){const retryable=v.review_status==='PENDING'&&['NETWORK_TIMEOUT','RATE_LIMIT','PROVIDER_5XX','INVALID_JSON','INVALID_SCHEMA'].includes(v.processing_error_code);const terminalHint=v.review_status==='REJECTED'?'该版本已被人工退回；请由负责人或协作者提交修订后的新版本。':'失败原因由系统错误码定位；这不会替代负责人的人工验收决定。';return `<div class="notice error"><b>辅助处理失败：${esc(v.processing_error_code)}</b><div>${esc(v.processing_error_stage)} · ${esc(v.processing_error_detail)}</div><div class="muted">${esc(terminalHint)}</div>${retryable?`<div class="actions"><button onclick="retryTaskProcessing('${v.version_id}')">修复后重新处理</button></div>`:''}</div>`}const p=v.processing_result||{},alignment=p.task_alignment||{},advice=p.acceptance_advice||{};return `<div class="notice"><b>验收辅助：${esc(alignment.status)} · 置信度 ${esc(alignment.confidence)}</b><div>${esc(alignment.reason)}</div></div>${p.evidence_digest?`<div class="quote"><b>${esc(p.evidence_digest.title)}</b><div>${esc(p.evidence_digest.summary)}</div></div>`:''}${p.gaps?.length?`<details><summary>可观察缺口（${p.gaps.length}）</summary>${p.gaps.map(g=>`<div class="activity-item"><b>${esc(g.severity)}</b> · ${esc(g.issue)}</div>`).join('')}</details>`:''}<div class="meta"><span>模型建议：${esc(advice.decision||'无')}</span><span>该建议不执行验收决定</span></div>`}
function reviewCard(t,reviewable){const m=t.proposal_metadata||{},targets=reviewable.filter(x=>x.action_item_id!==t.action_item_id),participants=(state?.participants||[]).filter(x=>(x.roles||[]).some(role=>['PARTICIPANT','ACTION_OWNER'].includes(role))),current=t.current_assignments||[],defaultOwner=current.find(x=>x.assignment_role==='OWNER')?.actor_id||m.collaboration_structure?.required_owner_actor_id||'',defaultCollaborators=new Set(current.filter(x=>x.assignment_role==='COLLABORATOR').map(x=>x.actor_id).concat(m.collaborator_actor_ids||[])),returned=current.find(x=>x.response_status==='RETURNED'),canIgnore=!t.published_sim_time&&t.status==='PENDING_CONFIRMATION';return `<article class="card task"><div class="row between"><h3>${t.status==='NEEDS_REVISION'?'修改退回任务':'复核并派发任务'}</h3><span class="pill ${t.status==='NEEDS_REVISION'?'bad':''}">${esc(phase(t))}</span></div>${returned?`<div class="notice error"><b>${esc(returned.display_name)} 退回重改：</b>${esc(returned.response_message)}</div>`:''}<div class="form-grid"><div class="wide"><label>任务标题</label><input data-draft="review-${t.action_item_id}-title" value="${esc(draft(`review-${t.action_item_id}-title`,t.title))}"></div><div><label>交付物</label><textarea data-draft="review-${t.action_item_id}-deliverable">${esc(draft(`review-${t.action_item_id}-deliverable`,m.deliverable||''))}</textarea></div><div><label>团队需要时间</label><input data-draft="review-${t.action_item_id}-team" type="datetime-local" value="${esc(draft(`review-${t.action_item_id}-team`,String(t.team_required_by_sim_time||'').slice(0,16)))}"></div><div class="wide"><label>执行人可见的工作要求</label><textarea data-draft="review-${t.action_item_id}-work">${esc(draft(`review-${t.action_item_id}-work`,m.work_requirements||m.deliverable||''))}</textarea></div><div class="wide"><label>仅负责人可见的验收规则</label><textarea data-draft="review-${t.action_item_id}-policy">${esc(draft(`review-${t.action_item_id}-policy`,m.management_review_policy||m.acceptance_criteria||''))}</textarea></div><div><label>优先级</label><select data-draft="review-${t.action_item_id}-priority">${['P0','P1','P2'].map(p=>`<option ${draft(`review-${t.action_item_id}-priority`,m.priority||'P1')===p?'selected':''}>${p}</option>`).join('')}</select></div>${canIgnore&&targets.length?`<div><label>合并到</label><select id="merge-${t.action_item_id}"><option value="">选择目标任务</option>${targets.map(x=>`<option value="${x.action_item_id}">${esc(x.title)}</option>`).join('')}</select></div>`:''}</div>${evidence(t)}<div class="form-grid"><div><label>主负责人</label><select data-draft="dispatch-${t.action_item_id}-owner"><option value="">请选择</option>${participants.map(p=>`<option value="${esc(p.actor_id)}" ${draft(`dispatch-${t.action_item_id}-owner`,defaultOwner)===p.actor_id?'selected':''}>${esc(p.display_name)}</option>`).join('')}</select></div><div><label>派发留言</label><input data-draft="dispatch-${t.action_item_id}-message" value="${esc(draft(`dispatch-${t.action_item_id}-message`,current[0]?.assignment_message||''))}" placeholder="说明分工或需要注意的事项"></div><div class="wide"><label>协作者（可多选）</label><div class="row">${participants.map(p=>`<label><input style="width:auto" type="checkbox" data-dispatch-collaborator="${t.action_item_id}" value="${esc(p.actor_id)}" ${defaultCollaborators.has(p.actor_id)?'checked':''}> ${esc(p.display_name)}</label>`).join('')}</div></div></div><div class="actions"><button class="secondary" onclick="saveProposal('${t.action_item_id}')">只保存修改</button><button class="good" onclick="dispatchTask('${t.action_item_id}')">${t.status==='NEEDS_REVISION'?'修改后重新派发':'保存并派发'}</button>${canIgnore?`<button class="danger" onclick="ignoreTask('${t.action_item_id}')">忽略</button>${targets.length?`<button class="secondary" onclick="mergeTask('${t.action_item_id}')">合并</button>`:''}`:''}</div></article>`}
function acceptanceCard(t){const v=t.latest_version,p=v?.payload||{},waiting=['PENDING','PROCESSING','RETRY_WAIT'].includes(v?.processing_status);return `<article class="card task"><div class="row between"><h3>${esc(t.title)}</h3><span class="pill warn">待验收</span></div><div class="meta"><span>提交人：${esc(v?.submitted_by_display_name||t.owner_display_name)}</span><span>任务负责人：${esc(t.owner_display_name)}</span><span>版本：${esc(v?.version_id)}</span><span>处理：${esc(v?.processing_status)}</span></div><div><b>结果摘要：</b>${esc(p.summary)}</div>${p.content?`<div class="quote">${esc(p.content)}</div>`:''}${p.links?.length?`<div><b>链接：</b>${p.links.map(x=>`<div>${esc(x)}</div>`).join('')}</div>`:''}${p.files?.length?`<div><b>附件：</b>${p.files.map(x=>`${esc(x.name)} · ${esc(x.extraction_status)}`).join('、')}</div>`:''}${processingReview(v)}<label>完成报告（验收后冻结，可在此整理）</label><textarea data-draft="accept-${v.version_id}-report">${esc(draft(`accept-${v.version_id}-report`,p.summary||p.completion_note||''))}</textarea><label>验收意见（退回时必填）</label><textarea data-draft="accept-${v.version_id}-comment">${esc(draft(`accept-${v.version_id}-comment`))}</textarea><div class="actions"><button class="good" ${waiting?'disabled':''} onclick="reviewVersion('${v.version_id}',true)">验收通过并冻结结果</button><button class="danger" ${waiting?'disabled':''} onclick="reviewVersion('${v.version_id}',false)">退回修改</button></div></article>`}
function renderManage(d){const proposals=d.tasks.filter(t=>['PENDING_CONFIRMATION','NEEDS_REVISION'].includes(t.status)&&!t.owner_actor_id),responding=d.tasks.filter(t=>t.status==='PENDING_ASSIGNMENT'),pending=d.tasks.filter(t=>t.status==='PENDING_ACCEPTANCE'),active=d.tasks.filter(t=>!['PENDING_CONFIRMATION','NEEDS_REVISION','REJECTED'].includes(t.status)),required=d.tasks.filter(t=>Boolean(t.required)),ready=required.length>0&&required.every(t=>['ACCEPTED','AGGREGATED','ARCHIVED'].includes(t.status)&&t.current_valid_version_id),canAggregate=ready&&(!d.final||d.final.status==='SUPERSEDED');document.querySelector('#app').innerHTML=`<div class="top"><div><div class="eyebrow">COORDINATOR</div><h1>会议任务管理</h1><div class="muted">先配置主负责人和协作者；全部成员接受后进入执行，协作贡献仍由主负责人整理。</div></div><div class="row"><span class="pill warn">${responding.length} 条待成员响应</span><span class="pill">${pending.length} 条待验收</span></div></div><section><h2>任务复核、修改与派发</h2><div class="grid">${proposals.length?proposals.map(t=>reviewCard(t,proposals.filter(x=>!x.published_sim_time))).join(''):'<div class="empty">没有待复核或退回重改的任务</div>'}</div></section><section class="section"><h2>交付验收</h2><div class="grid">${pending.length?pending.map(acceptanceCard).join(''):'<div class="empty">当前没有待验收交付</div>'}</div></section><section class="section"><h2>终稿汇总</h2><div class="card">${finalCard(d,canAggregate)}</div></section><section class="section"><h2>执行与派发概览</h2><div class="grid two">${active.length?active.map(t=>`<article class="card">${taskHeader(t)}${validationNotice(t)}${contributionPanel(t)}${activityPanel(t)}${t.latest_progress?`<div class="quote"><b>最近进展：</b>${esc(t.latest_progress.payload.progress_summary)}</div>`:''}</article>`).join(''):'<div class="empty">派发后任务会显示在这里</div>'}</div></section><section class="section"><h2>需要负责人决定</h2><div class="grid">${d.pending_approvals.length?d.pending_approvals.map(a=>`<div class="card"><b>终稿发布</b><div class="muted">批准后终稿将通过 mock IM 发布并归档；驳回必须说明需要哪项任务成果修改。</div><label>驳回反馈（批准时可不填）</label><textarea data-draft="approval-${a.approval_id}-comment">${esc(draft(`approval-${a.approval_id}-comment`))}</textarea><div class="actions"><button onclick="decideApproval('${a.approval_id}',true)">批准</button><button class="danger" onclick="decideApproval('${a.approval_id}',false)">驳回</button></div></div>`).join(''):'<div class="empty">当前没有待发布终稿</div>'}</div></section>`}
async function decideApproval(id,approve){const comment=String(field(`approval-${id}-comment`)||'').trim();if(!approve&&!comment){flash('驳回终稿时请说明需要修改的任务成果',true);return}try{await post(`/api/approvals/${id}`,{approve,comment});clearDraftPrefix(`approval-${id}`);flash(approve?'已批准并进入发布':'已驳回，终稿不会发布');await load(true)}catch(e){flash(e.message,true)}}
function renderDiagnostics(d){const trace=d.agent_trace||[];document.querySelector('#app').innerHTML=`<div class="top"><div><div class="eyebrow">AGENT TRACE</div><h1>Agent 运行与评测证据</h1><div class="muted">只显示调用目的、版本引用、上下文预算、Token usage 与状态，不展示完整 Prompt 或附件正文。</div></div></div><section class="grid gates">${Object.entries(d.report.gates).map(([k,v])=>`<div class="card gate ${v.passed?'':'fail'}"><b>${v.passed?'通过':'未通过'}</b><div>${esc(k)}</div></div>`).join('')}</section><section class="section"><h2>Context / Token Trace</h2><div class="grid">${trace.length?trace.slice().reverse().map(t=>`<article class="card"><div class="row between"><b>#${esc(t.sequence_no)} ${esc(t.event_type)}</b><span class="pill ${t.output_status==='FAILED'?'bad':'ok'}">${esc(t.output_status||t.step_kind||'RECORDED')}</span></div><div class="meta"><span>Purpose: ${esc(t.purpose||'AGENT_LOOP')}</span><span>Model: ${esc(t.model||'deterministic')}</span><span>估算 Token: ${esc(t.estimated_input_tokens??'N/A')} / ${esc(t.token_budget??'N/A')}</span><span>实际 Token: ${esc(t.total_tokens??'N/A')}</span></div><div class="muted">包含 ${esc((t.included_refs||[]).length)} 个引用 · 省略 ${esc((t.omitted_refs||[]).length)} 个引用 · ${esc(t.aggregate_id)}</div></article>`).join(''):'<div class="empty">尚无 Agent 调用记录</div>'}</div></section><section class="grid two section"><div class="card"><h2>流程 / 效果 / 单点指标</h2><pre>${esc(JSON.stringify(d.report.signals,null,2))}</pre></div><div class="card"><h2>全局审计时间线</h2>${d.timeline.slice().reverse().map(e=>`<div class="event"><b>#${e.sequence_no} ${esc(e.event_type)}</b><br><span>${esc(e.aggregate_type)} · ${esc(e.aggregate_id)}</span><br><span class="muted">${esc(e.sim_time)}</span></div>`).join('')}</div></section>`}
function renderManageP1(d){renderManage(d);const html=structureManager(d)+structureRevokePanel(d),top=document.querySelector('#app .top');if(html&&top)top.insertAdjacentHTML('afterend',html)}
function render(d){if(route==='manage')renderManageP1(d);else if(route==='diagnostics')renderDiagnostics(d);else renderTasks(d)}
async function load(force=false,authRetry=true){if(!sessionToken){document.querySelector('#app').innerHTML='<div class=\"empty\">请先在右上角选择本次会议身份</div>';return}try{const response=await fetch(`/api/state?surface=${encodeURIComponent(route)}`,{headers:authHeaders()});const d=await response.json();if(!response.ok){if(response.status===401){sessionToken='';localStorage.removeItem('collabSessionToken');if(authRetry&&actorSelect.value){await issueSession(actorSelect.value);return load(force,false)}}throw new Error(d.message||'读取工作台失败')}state=d;document.querySelectorAll('[data-route]').forEach(a=>a.classList.toggle('hidden',!(d.allowed_surfaces||[]).includes(a.dataset.route)));const editing=['INPUT','TEXTAREA','SELECT'].includes(document.activeElement?.tagName);const pendingFiles=[...document.querySelectorAll('input[type=file]')].some(input=>input.files?.length);if(force||(!editing&&!pendingFiles))render(d)}catch(e){flash('读取工作台失败：'+e.message,true)}}
async function initializeIdentity(){try{const response=await fetch('/api/session/actors');const result=await response.json();const actors=result.actors||[];actorSelect.innerHTML='<option value="">请选择本次参会者</option>'+actors.map(actor=>`<option value="${esc(actor.actor_id)}">${esc(actor.display_name)} · ${esc(actor.roles.join('/'))}</option>`).join('');const stored=localStorage.getItem('collabActorId')||'';if(actors.some(actor=>actor.actor_id===stored))actorSelect.value=stored;if(actorSelect.value){if(!sessionToken)await issueSession(actorSelect.value);await load(true)}else if(actors.length===1){actorSelect.value=actors[0].actor_id;actorSelect.dispatchEvent(new Event('change'))}else{document.querySelector('#app').innerHTML='<div class="empty">请先在右上角选择本次会议身份</div>'}}catch(e){flash('身份列表载入失败：'+e.message,true)}}
initializeIdentity();
</script></body></html>"""

# The page and the server must agree on one set of upload limits, so the
# client constants are substituted from the same module that enforces them.
WORKBENCH_HTML = WORKBENCH_HTML.replace(
    "__MAX_ATTACHMENT_COUNT__", str(MAX_ATTACHMENT_COUNT)
).replace(
    "__ATTACHMENT_LIMIT_MB__", str(MAX_TOTAL_ATTACHMENT_BYTES // (1024 * 1024))
).replace(
    # The same list the Feishu card offers. Substituted rather than duplicated
    # so a reason can never exist on one surface and not the other.
    "__RETURN_REASONS__", json.dumps(list(ASSIGNMENT_RETURN_REASONS), ensure_ascii=False)
).replace(
    "__OTHER_REASON__", json.dumps(OTHER_RETURN_REASON, ensure_ascii=False)
)


def _decode_json(value: Any) -> Any:
    if isinstance(value, str):
        return json.loads(value)
    return value


def _agent_trace(service: CoordinationService) -> list[dict[str, Any]]:
    event_types = (
        "TaskResultProcessingStarted",
        "TaskResultProcessingSucceeded",
        "TaskResultProcessingFailed",
        "TaskResultProcessingRecovered",
        "FinalOrganizationQueued",
        "FinalOrganizationStarted",
        "FinalOrganizationSucceeded",
        "FinalOrganizationRetryScheduled",
        "QuestionBallotDraftPrepared",
        "CollaborationHintContextBuilt",
        "AgentStepCompleted",
        "ProcessRecovered",
    )
    placeholders = ", ".join("?" for _ in event_types)
    rows = service.db.all(
        "SELECT sequence_no, aggregate_id, event_type, payload, sim_time "
        "FROM audit_events WHERE run_id = ? "
        f"AND event_type IN ({placeholders}) ORDER BY sequence_no",
        (service.run_id, *event_types),
    )
    trace: list[dict[str, Any]] = []
    for row in rows:
        payload = _decode_json(row["payload"])
        invocation = payload.get("invocation") or {}
        if not invocation and payload.get("generation"):
            generation = payload["generation"]
            invocation = {
                "purpose": "QUESTION_BALLOT_DRAFT",
                "output_status": "SUCCEEDED",
                "model": generation.get("model"),
                "context": generation.get("context_manifest") or {},
                "usage": generation.get("usage") or {},
            }
        context = invocation.get("context") or {}
        usage = invocation.get("usage") or {}
        trace.append(
            {
                "sequence_no": int(row["sequence_no"]),
                "aggregate_id": row["aggregate_id"],
                "event_type": row["event_type"],
                "sim_time": row["sim_time"],
                "purpose": invocation.get("purpose"),
                "output_status": invocation.get("output_status"),
                "model": invocation.get("model"),
                "estimated_input_tokens": context.get(
                    "estimated_input_tokens"
                ),
                "token_budget": context.get("token_budget"),
                "included_refs": context.get("included_refs") or [],
                "omitted_refs": context.get("omitted_refs") or [],
                "truncation_strategy": context.get("truncation_strategy") or [],
                "total_tokens": usage.get("total_tokens"),
                "step_kind": payload.get("step_kind"),
            }
        )
    return trace


def _task_activity(
    service: CoordinationService,
    action: dict[str, Any],
    *,
    audit_sequences: dict[str, int],
) -> list[dict[str, Any]]:
    db = service.db
    action_id = action["action_item_id"]
    actor_names = {
        row["actor_id"]: row["display_name"]
        for row in db.all("SELECT actor_id, display_name FROM actors")
    }
    activity: list[dict[str, Any]] = []

    assignments = db.all(
        "SELECT * FROM action_item_assignments WHERE action_item_id = ? "
        "ORDER BY definition_version, assigned_sim_time, actor_id",
        (action_id,),
    )
    for assignment in assignments:
        role_label = (
            "主负责人"
            if assignment["assignment_role"] == "OWNER"
            else "协作者"
        )
        activity.append(
            {
                "kind": "ASSIGNMENT",
                "title": f"派发任务 v{assignment['definition_version']}",
                "detail": (
                    f"派发为{role_label}"
                    + (
                        f"；留言：{assignment['assignment_message']}"
                        if assignment["assignment_message"]
                        else ""
                    )
                ),
                "status": "PENDING",
                "actor": actor_names.get(
                    assignment["actor_id"], assignment["actor_id"]
                ),
                "actor_id": assignment["actor_id"],
                "sim_time": assignment["assigned_sim_time"],
                "sequence_no": audit_sequences.get(action_id, 0),
            }
        )
        response_events = db.all(
            "SELECT * FROM audit_events WHERE run_id = ? AND aggregate_id = ? "
            "AND event_type IN ('ActionItemAssignmentAccepted',"
            "'ActionItemAssignmentReturned') ORDER BY sequence_no",
            (service.run_id, assignment["assignment_id"]),
        )
        for event in response_events:
            payload = _decode_json(event["payload"])
            returned = event["event_type"] == "ActionItemAssignmentReturned"
            activity.append(
                {
                    "kind": "ASSIGNMENT",
                    "title": "退回任务定义" if returned else "接受任务派发",
                    "detail": payload.get("reason")
                    or payload.get("message")
                    or ("需要负责人重改" if returned else f"以{role_label}身份接受"),
                    "status": "RETURNED" if returned else "ACCEPTED",
                    "actor": actor_names.get(
                        assignment["actor_id"], assignment["actor_id"]
                    ),
                    "actor_id": assignment["actor_id"],
                    "sim_time": event["sim_time"],
                    "sequence_no": event["sequence_no"],
                }
            )

    revisions = db.all(
        "SELECT * FROM commitment_revisions WHERE action_item_id = ? "
        "ORDER BY revision_no",
        (action_id,),
    )
    for revision in revisions:
        activity.append(
            {
                "kind": "COMMITMENT",
                "title": (
                    "确认任务承诺"
                    if int(revision["revision_no"]) == 1
                    else "更新任务承诺"
                ),
                "detail": f'承诺截止：{revision["promised_deadline_sim_time"]}',
                "status": revision["status"],
                # The same instant the prose carries, kept apart from it: the
                # schedule bar draws a superseded promise where it used to sit,
                # and digging a date back out of a sentence would break the
                # first time the sentence is reworded.
                "promised_deadline_sim_time": revision[
                    "promised_deadline_sim_time"
                ],
                "revision_no": int(revision["revision_no"]),
                "actor": actor_names.get(
                    revision["owner_actor_id"], revision["owner_actor_id"]
                ),
                "actor_id": revision["owner_actor_id"],
                "sim_time": revision["created_sim_time"],
                "sequence_no": audit_sequences.get(
                    revision["commitment_revision_id"], 0
                ),
            }
        )

    versions = db.all(
        "SELECT * FROM artifact_versions WHERE action_item_id = ? "
        "ORDER BY received_sequence",
        (action_id,),
    )
    for version in versions:
        payload = _decode_json(version["payload"])
        errors = _decode_json(version["validation_errors"])
        missing = errors.get("missing_fields", [])
        is_collaborator_submission = bool(
            version["submitted_by_actor_id"]
            and version["submitted_by_actor_id"] != action.get("owner_actor_id")
        )
        if version["validation_status"] == "FAILED":
            status = "VALIDATION_FAILED"
            outcome = "校验未通过：" + "、".join(missing)
        elif version["review_status"] == "PENDING":
            status = "PENDING_ACCEPTANCE"
            outcome = "格式校验通过，等待负责人验收"
        elif version["review_status"] == "REJECTED":
            status = "REJECTED"
            outcome = f'负责人退回：{version["review_comment"] or "未填写原因"}'
        elif version["review_status"] == "ACCEPTED":
            status = "ACCEPTED"
            outcome = f'负责人验收通过：{version["review_comment"] or "无补充意见"}'
        elif is_collaborator_submission:
            status = "CONTRIBUTION_RECEIVED"
            outcome = "协作贡献已接收，等待任务负责人处理"
        else:
            status = version["validation_status"]
            outcome = "格式校验通过"
        summary = payload.get("summary") or payload.get("content") or "未填写摘要"
        receipt_event = db.one(
            "SELECT sequence_no, sim_time FROM audit_events WHERE run_id = ? "
            "AND aggregate_id = ? AND event_type = 'ArtifactVersionReceived' "
            "ORDER BY sequence_no LIMIT 1",
            (service.run_id, version["version_id"]),
        )
        activity.append(
            {
                "kind": "DELIVERY",
                "title": (
                    f'协作者提交贡献版本 {version["received_sequence"]}'
                    if is_collaborator_submission
                    else f'提交交付版本 {version["received_sequence"]}'
                ),
                "detail": f'{summary}；{outcome}',
                "status": status,
                "actor": actor_names.get(
                    version["submitted_by_actor_id"]
                    or action.get("owner_actor_id"),
                    version["submitted_by_actor_id"]
                    or action.get("owner_actor_id"),
                ),
                "actor_id": version["submitted_by_actor_id"]
                or action.get("owner_actor_id"),
                "sim_time": (
                    receipt_event["sim_time"]
                    if receipt_event
                    else version["received_sim_time"]
                ),
                "sequence_no": (
                    receipt_event["sequence_no"] if receipt_event else 0
                ),
                "version_id": version["version_id"],
            }
        )

        decision_titles = {
            "ArtifactContributionIncluded": "负责人已纳入协作资料",
            "ArtifactContributionRevisionRequested": "负责人要求补充协作成果",
            "ArtifactContributionPromotedToFinalCandidate": "负责人将贡献送入任务验收",
            "ArtifactContributionReclassified": "协作成果已恢复为贡献版本",
        }
        decision_events = db.all(
            "SELECT * FROM audit_events WHERE run_id = ? AND aggregate_id = ? "
            "AND event_type IN ('ArtifactContributionIncluded',"
            "'ArtifactContributionRevisionRequested',"
            "'ArtifactContributionPromotedToFinalCandidate',"
            "'ArtifactContributionReclassified') ORDER BY sequence_no",
            (service.run_id, version["version_id"]),
        )
        for event in decision_events:
            decision_payload = _decode_json(event["payload"])
            actor_id = decision_payload.get("decided_by")
            activity.append(
                {
                    "kind": "CONTRIBUTION",
                    "title": decision_titles[event["event_type"]],
                    "detail": decision_payload.get("comment")
                    or decision_payload.get("reason")
                    or "协作成果状态已更新",
                    "status": decision_payload.get("contribution_status")
                    or "AWAITING_OWNER",
                    "actor": actor_names.get(actor_id, actor_id or "SYSTEM"),
                    "actor_id": actor_id,
                    "sim_time": event["sim_time"],
                    "sequence_no": event["sequence_no"],
                    "version_id": version["version_id"],
                }
            )

        review_titles = {
            "ArtifactVersionAcceptedByCoordinator": "任务成果验收通过",
            "ArtifactVersionReturnedForRevision": "任务成果被退回",
        }
        review_events = db.all(
            "SELECT * FROM audit_events WHERE run_id = ? AND aggregate_id = ? "
            "AND event_type IN ('ArtifactVersionAcceptedByCoordinator',"
            "'ArtifactVersionReturnedForRevision') ORDER BY sequence_no",
            (service.run_id, version["version_id"]),
        )
        for event in review_events:
            review_payload = _decode_json(event["payload"])
            reviewer = review_payload.get("reviewed_by")
            activity.append(
                {
                    "kind": "REVIEW",
                    "title": review_titles[event["event_type"]],
                    "detail": review_payload.get("comment") or "未补充意见",
                    "status": (
                        "ACCEPTED"
                        if event["event_type"]
                        == "ArtifactVersionAcceptedByCoordinator"
                        else "REJECTED"
                    ),
                    "actor": actor_names.get(reviewer, reviewer),
                    "actor_id": reviewer,
                    "sim_time": event["sim_time"],
                    "sequence_no": event["sequence_no"],
                    "version_id": version["version_id"],
                }
            )

    interventions = db.all(
        "SELECT * FROM interventions WHERE action_item_id = ? "
        "ORDER BY created_sim_time, intervention_id",
        (action_id,),
    )
    for intervention in interventions:
        activity.append(
            {
                "kind": "INTERVENTION",
                "title": f'{intervention["level"]} 协调触达',
                "detail": f'原因：{intervention["reason_code"]}',
                "status": intervention["status"],
                "actor": actor_names.get(
                    intervention["target_actor_id"],
                    intervention["target_actor_id"],
                ),
                "actor_id": intervention["target_actor_id"],
                "sim_time": intervention["created_sim_time"],
                "sequence_no": audit_sequences.get(
                    intervention["intervention_id"], 0
                ),
            }
        )

    progress_events = db.all(
        "SELECT * FROM audit_events WHERE run_id = ? AND aggregate_id = ? "
        "AND event_type = 'ActionItemProgressUpdated' ORDER BY sequence_no",
        (service.run_id, action_id),
    )
    for event in progress_events:
        payload = _decode_json(event["payload"])
        detail = payload.get("progress_summary", "未填写进展")
        if payload.get("blocked"):
            detail += f'；阻塞：{payload.get("blocker_reason", "未填写")}'
        if payload.get("next_step"):
            detail += f'；下一步：{payload["next_step"]}'
        activity.append(
            {
                "kind": "PROGRESS",
                "title": "更新任务进展",
                "detail": detail,
                "status": "BLOCKED" if payload.get("blocked") else "UPDATED",
                "actor": actor_names.get(
                    payload.get("updated_by"), payload.get("updated_by")
                ),
                "actor_id": payload.get("updated_by"),
                "sim_time": event["sim_time"],
                "sequence_no": event["sequence_no"],
            }
        )

    signal_events = db.all(
        "SELECT * FROM audit_events WHERE run_id = ? AND aggregate_id = ? "
        "AND event_type = 'ProgressSignalRecorded' ORDER BY sequence_no",
        (service.run_id, action_id),
    )
    signal_titles = {
        "ON_TRACK": "状态更新：按计划",
        "AT_RISK": "状态更新：有风险",
        "BLOCKED": "状态更新：被阻塞",
        "WAITING_INPUT": "状态更新：等待输入",
        "READY_TO_SUBMIT": "状态更新：准备提交",
    }
    for event in signal_events:
        payload = _decode_json(event["payload"])
        signal_type = payload.get("signal_type")
        if signal_type not in signal_titles:
            continue
        activity.append(
            {
                "kind": "STATUS",
                "title": signal_titles[signal_type],
                "detail": payload.get("note") or "未补充说明",
                "status": signal_type,
                "actor": actor_names.get(
                    payload.get("actor_id"), payload.get("actor_id")
                ),
                "actor_id": payload.get("actor_id"),
                "sim_time": event["sim_time"],
                "sequence_no": event["sequence_no"],
            }
        )

    assistance_rows = db.all(
        "SELECT * FROM assistance_requests WHERE action_item_id = ? "
        "ORDER BY created_sim_time, assistance_request_id",
        (action_id,),
    )
    assistance_titles = {
        "AssistanceRequested": "邀请协作者",
        "AssistanceAcknowledged": "协作者确认接手",
        "AssistanceResolved": "协作已完成",
        "AssistanceCancelled": "协作已取消",
    }
    for request in assistance_rows:
        events = db.all(
            "SELECT * FROM audit_events WHERE run_id = ? AND aggregate_id = ? "
            "AND event_type IN ('AssistanceRequested','AssistanceAcknowledged',"
            "'AssistanceResolved','AssistanceCancelled') ORDER BY sequence_no",
            (service.run_id, request["assistance_request_id"]),
        )
        for event in events:
            payload = _decode_json(event["payload"])
            event_type = event["event_type"]
            if event_type == "AssistanceRequested":
                actor_id = payload.get("requester_actor_id")
                detail = (
                    f'邀请 {actor_names.get(payload.get("target_actor_id"), payload.get("target_actor_id"))}'
                    f' 协作：{payload.get("summary") or request["summary"]}'
                )
                status = "OPEN"
            else:
                actor_id = payload.get("actor_id")
                detail = payload.get("resolution_summary") or (
                    "已进入同一任务协作空间"
                    if event_type == "AssistanceAcknowledged"
                    else "协作状态已更新"
                )
                status = payload.get("status") or request["status"]
            activity.append(
                {
                    "kind": "COLLABORATION",
                    "title": assistance_titles[event_type],
                    "detail": detail,
                    "status": status,
                    "actor": actor_names.get(actor_id, actor_id),
                    "actor_id": actor_id,
                    "sim_time": event["sim_time"],
                    "sequence_no": event["sequence_no"],
                }
            )

    return sorted(
        activity,
        key=lambda item: (item["sim_time"], int(item["sequence_no"])),
        reverse=True,
    )


def workbench_state(
    service: CoordinationService,
    *,
    result_processing_mode: str = "local",
    principal: Principal | None = None,
) -> dict[str, Any]:
    db = service.db
    episode = db.one("SELECT * FROM episodes WHERE episode_id = ?", (service.episode_id,))
    timeline = service.audit_events()
    audit_sequences: dict[str, int] = {}
    for event in timeline:
        audit_sequences[event["aggregate_id"]] = max(
            audit_sequences.get(event["aggregate_id"], 0),
            int(event["sequence_no"]),
        )
    tasks: list[dict[str, Any]] = []
    for row in service.action_items():
        assignment_rows = db.all(
            "SELECT aa.*, actor.display_name FROM action_item_assignments aa "
            "JOIN actors actor ON actor.actor_id = aa.actor_id "
            "WHERE aa.action_item_id = ? "
            "ORDER BY aa.definition_version DESC, "
            "CASE aa.assignment_role WHEN 'OWNER' THEN 0 ELSE 1 END, "
            "actor.display_name, aa.actor_id",
            (row["action_item_id"],),
        )
        row["assignments"] = [dict(item) for item in assignment_rows]
        row["current_assignments"] = [
            dict(item)
            for item in assignment_rows
            if int(item["definition_version"])
            == int(row.get("definition_version") or 1)
        ]
        row["collaboration_hints"] = []
        if row["status"] in {"TRACKING", "PENDING_ACCEPTANCE"}:
            for assignment in row["current_assignments"]:
                if assignment["response_status"] != "ACCEPTED":
                    continue
                memory_rows = db.all(
                    "SELECT topic, value FROM collaboration_memories "
                    "WHERE actor_id = ? AND status = 'CONFIRMED' "
                    "ORDER BY topic, confirmed_sim_time, memory_id",
                    (assignment["actor_id"],),
                )
                for memory in memory_rows:
                    value = _decode_json(memory["value"]) or {}
                    row["collaboration_hints"].append(
                        {
                            "actor_id": assignment["actor_id"],
                            "display_name": assignment["display_name"],
                            "topic": memory["topic"],
                            "code": value.get("code"),
                            "statement": value.get("statement"),
                        }
                    )
        active_commitment = db.one(
            "SELECT * FROM commitment_revisions WHERE "
            "commitment_revision_id = ? AND status = 'ACTIVE'",
            (row["active_commitment_revision_id"],),
        ) if row["active_commitment_revision_id"] else None
        latest_intervention = db.one(
            "SELECT level, action_type, created_sim_time, status FROM interventions "
            "WHERE action_item_id = ? ORDER BY created_sim_time DESC LIMIT 1",
            (row["action_item_id"],),
        )
        latest_progress = db.one(
            "SELECT payload, sim_time FROM audit_events WHERE run_id = ? "
            "AND aggregate_id = ? AND event_type = 'ActionItemProgressUpdated' "
            "ORDER BY sequence_no DESC LIMIT 1",
            (service.run_id, row["action_item_id"]),
        )
        all_versions = db.all(
            "SELECT * FROM artifact_versions WHERE action_item_id = ? "
            "ORDER BY received_sequence DESC",
            (row["action_item_id"],),
        )
        latest_version = all_versions[0] if all_versions else None
        current_version = (
            db.one(
                "SELECT * FROM artifact_versions WHERE version_id = ?",
                (row["current_valid_version_id"],),
            )
            if row["current_valid_version_id"]
            else None
        )
        assistance_rows = db.all(
            "SELECT ar.*, requester.display_name AS requester_display_name, "
            "target.display_name AS target_display_name "
            "FROM assistance_requests ar "
            "JOIN actors requester ON requester.actor_id = ar.requester_actor_id "
            "JOIN actors target ON target.actor_id = ar.target_actor_id "
            "WHERE ar.action_item_id = ? "
            "ORDER BY ar.created_sim_time DESC, ar.assistance_request_id DESC",
            (row["action_item_id"],),
        )
        row["assistance_requests"] = [dict(item) for item in assistance_rows]
        row["active_assistance"] = next(
            (
                dict(item)
                for item in assistance_rows
                if item["status"] in ("OPEN", "ACKNOWLEDGED")
            ),
            None,
        )
        metadata = service.proposal_metadata(row)
        collaborator_sources: dict[str, set[str]] = {}
        historical_collaborator_sources: dict[str, set[str]] = {}
        if assignment_rows:
            if row["status"] in {
                "TRACKING",
                "PENDING_ACCEPTANCE",
                "ACCEPTED",
                "AGGREGATED",
                "ARCHIVED",
            }:
                for assignment in row["current_assignments"]:
                    if (
                        assignment["assignment_role"] == "COLLABORATOR"
                        and assignment["response_status"] == "ACCEPTED"
                    ):
                        collaborator_sources.setdefault(
                            assignment["actor_id"], set()
                        ).add("DISPATCH_ACCEPTED")
                        historical_collaborator_sources.setdefault(
                            assignment["actor_id"], set()
                        ).add("DISPATCH_ACCEPTED")
        else:
            # Backward-compatible read for meetings created before ADR-035.
            for actor_id in metadata.get("collaborator_actor_ids", []):
                collaborator_sources.setdefault(actor_id, set()).add(
                    "MEETING_RECORDED"
                )
                historical_collaborator_sources.setdefault(actor_id, set()).add(
                    "MEETING_RECORDED"
                )
        for request in assistance_rows:
            historical_collaborator_sources.setdefault(
                request["target_actor_id"], set()
            ).add("ASSISTANCE_HISTORY")
            if request["status"] in ("OPEN", "ACKNOWLEDGED"):
                collaborator_sources.setdefault(
                    request["target_actor_id"], set()
                ).add("ACTIVE_REQUEST")
        for version in all_versions:
            submitter = version["submitted_by_actor_id"]
            if submitter and submitter != row.get("owner_actor_id"):
                historical_collaborator_sources.setdefault(
                    submitter, set()
                ).add("CONTRIBUTION_SUBMITTED")
        collaborator_sources.pop(row.get("owner_actor_id"), None)
        historical_collaborator_sources.pop(row.get("owner_actor_id"), None)
        row["active_collaborator_actor_ids"] = sorted(collaborator_sources)
        row["historical_collaborator_actor_ids"] = sorted(
            historical_collaborator_sources
        )
        row["collaborators"] = [
            {
                "actor_id": actor_id,
                "display_name": next(
                    (
                        participant["target_display_name"]
                        for participant in assistance_rows
                        if participant["target_actor_id"] == actor_id
                    ),
                    None,
                )
                or db.one(
                    "SELECT display_name FROM actors WHERE actor_id = ?",
                    (actor_id,),
                )["display_name"],
                "sources": sorted(sources),
            }
            for actor_id, sources in sorted(collaborator_sources.items())
        ]
        row["historical_collaborators"] = [
            {
                "actor_id": actor_id,
                "display_name": (
                    db.one(
                        "SELECT display_name FROM actors WHERE actor_id = ?",
                        (actor_id,),
                    )["display_name"]
                ),
                "sources": sorted(sources),
            }
            for actor_id, sources in sorted(
                historical_collaborator_sources.items()
            )
        ]
        contribution_versions: list[dict[str, Any]] = []
        for version in all_versions:
            submitter = version["submitted_by_actor_id"]
            if not submitter or submitter == row.get("owner_actor_id"):
                continue
            item = dict(version)
            item["payload"] = _decode_json(version["payload"])
            item["validation_errors"] = _decode_json(version["validation_errors"])
            for field in (
                "attachment_extractions",
                "source_manifest",
                "processing_result",
                "processing_metadata",
            ):
                item[field] = _decode_json(version[field])
            submitter_row = db.one(
                "SELECT display_name FROM actors WHERE actor_id = ?",
                (submitter,),
            )
            item["submitted_by_display_name"] = (
                submitter_row["display_name"] if submitter_row else submitter
            )
            decision_event = db.one(
                "SELECT event_type, payload, sequence_no FROM audit_events "
                "WHERE run_id = ? AND aggregate_id = ? AND event_type IN "
                "('ArtifactContributionIncluded',"
                "'ArtifactContributionRevisionRequested',"
                "'ArtifactContributionPromotedToFinalCandidate') "
                "ORDER BY sequence_no DESC LIMIT 1",
                (service.run_id, version["version_id"]),
            )
            if version["validation_status"] == "FAILED":
                contribution_status = "VALIDATION_FAILED"
            elif version["review_status"] != "NOT_REQUIRED":
                contribution_status = "PROMOTED"
            elif decision_event:
                contribution_status = {
                    "ArtifactContributionIncluded": "INCLUDED",
                    "ArtifactContributionRevisionRequested": "REVISION_REQUESTED",
                    "ArtifactContributionPromotedToFinalCandidate": "PROMOTED",
                }[decision_event["event_type"]]
            else:
                contribution_status = "AWAITING_OWNER"
            item["contribution_status"] = contribution_status
            item["decision"] = (
                _decode_json(decision_event["payload"]) if decision_event else None
            )
            item["can_request_revision"] = bool(
                submitter in collaborator_sources
            )
            contribution_versions.append(item)
        row["contribution_versions"] = contribution_versions
        row["last_owner_signal"] = (
            {
                "signal_type": row.get("last_owner_signal_type"),
                "signal_at": row.get("last_owner_signal_at"),
                "valid_until": row.get("last_owner_signal_valid_until"),
            }
            if row.get("last_owner_signal_at")
            else None
        )
        row["last_intervention"] = (
            dict(latest_intervention) if latest_intervention else None
        )
        row["active_commitment"] = (
            dict(active_commitment) if active_commitment else None
        )
        row["promised_by_sim_time"] = (
            active_commitment["promised_deadline_sim_time"]
            if active_commitment
            else None
        )
        team_required_by = row.get("team_required_by_sim_time")
        promised_by = row["promised_by_sim_time"]
        complete = row["status"] in {
            "ACCEPTED",
            "AGGREGATED",
            "ARCHIVED",
            "REJECTED",
        }
        if promised_by and team_required_by and parse_time(promised_by) > parse_time(team_required_by):
            row["schedule_status"] = "CONFLICT"
            row["schedule_risk_reason"] = "个人承诺晚于团队需要时间"
        elif promised_by and not complete and parse_time(service.now()) > parse_time(promised_by):
            row["schedule_status"] = "OVERDUE"
            row["schedule_risk_reason"] = "已超过个人承诺时间"
        else:
            row["schedule_status"] = "ON_TIME"
            row["schedule_risk_reason"] = ""
        if latest_progress:
            row["latest_progress"] = {
                "payload": _decode_json(latest_progress["payload"]),
                "sim_time": latest_progress["sim_time"],
            }
        else:
            row["latest_progress"] = None
        if latest_version:
            row["latest_version"] = dict(latest_version)
            row["latest_version"]["payload"] = _decode_json(latest_version["payload"])
            row["latest_version"]["validation_errors"] = _decode_json(
                latest_version["validation_errors"]
            )
            for field in (
                "attachment_extractions",
                "source_manifest",
                "processing_result",
                "processing_metadata",
            ):
                row["latest_version"][field] = _decode_json(latest_version[field])
            row["latest_version"]["is_contribution"] = bool(
                latest_version["submitted_by_actor_id"]
                and latest_version["submitted_by_actor_id"]
                != row.get("owner_actor_id")
                and latest_version["review_status"] == "NOT_REQUIRED"
            )
            latest_submitter = latest_version["submitted_by_actor_id"]
            latest_submitter_row = (
                db.one(
                    "SELECT display_name FROM actors WHERE actor_id = ?",
                    (latest_submitter,),
                )
                if latest_submitter
                else None
            )
            row["latest_version"]["submitted_by_display_name"] = (
                latest_submitter_row["display_name"]
                if latest_submitter_row
                else row.get("owner_display_name")
            )
        else:
            row["latest_version"] = None
        if current_version:
            row["current_version"] = dict(current_version)
            row["current_version"]["payload"] = _decode_json(
                current_version["payload"]
            )
            row["current_version"]["validation_errors"] = _decode_json(
                current_version["validation_errors"]
            )
            for field in (
                "attachment_extractions",
                "source_manifest",
                "processing_result",
                "processing_metadata",
            ):
                row["current_version"][field] = _decode_json(current_version[field])
        else:
            row["current_version"] = None
        accepted_result = (
            db.one(
                "SELECT * FROM accepted_task_results WHERE action_item_id = ? "
                "AND accepted_version_id = ?",
                (row["action_item_id"], row["current_valid_version_id"]),
            )
            if row["current_valid_version_id"]
            else None
        )
        row["accepted_task_result"] = dict(accepted_result) if accepted_result else None
        if row["accepted_task_result"]:
            for field in (
                "completed_content_refs",
                "normalized_result",
                "source_manifest",
                "processing_metadata",
                "collaboration_report",
            ):
                row["accepted_task_result"][field] = _decode_json(
                    accepted_result[field]
                )
        row["proposal_metadata"] = metadata
        row["collaboration_progress"] = service.collaboration_progress(
            row["action_item_id"]
        )
        row["collaboration_inputs"] = (
            service.collaboration_input_context(row["action_item_id"])
            if row["collaboration_progress"]
            else None
        )
        owner = None
        if row["owner_actor_id"]:
            owner = db.one(
                "SELECT display_name FROM actors WHERE actor_id = ?",
                (row["owner_actor_id"],),
            )
        row["owner_display_name"] = owner["display_name"] if owner else None
        assigned_owner = next(
            (
                assignment
                for assignment in row["current_assignments"]
                if assignment["assignment_role"] == "OWNER"
            ),
            None,
        )
        row["assigned_owner_display_name"] = (
            assigned_owner["display_name"] if assigned_owner else None
        )
        row["activity"] = _task_activity(
            service, row, audit_sequences=audit_sequences
        )
        tasks.append(row)
    approvals = [
        {**dict(row), "requested_action": _decode_json(row["requested_action"])}
        for row in db.all(
            "SELECT * FROM approvals WHERE episode_id = ? "
            "AND approval_type = 'FINAL_RELEASE' AND status = 'PENDING' "
            "ORDER BY requested_sim_time",
            (service.episode_id,),
        )
    ]
    final = None
    lineage: list[dict[str, Any]] = []
    if episode["current_final_deliverable_id"]:
        final_row = db.one(
            "SELECT * FROM final_deliverables WHERE final_deliverable_id = ?",
            (episode["current_final_deliverable_id"],),
        )
        final = dict(final_row) if final_row else None
        if final:
            final["payload"] = _decode_json(final["payload"])
            final["release_review"] = None
            release_approvals = db.all(
                "SELECT * FROM approvals WHERE episode_id = ? "
                "AND approval_type = 'FINAL_RELEASE' "
                "ORDER BY requested_sim_time DESC",
                (service.episode_id,),
            )
            for release_approval in release_approvals:
                requested = _decode_json(release_approval["requested_action"])
                if requested.get("final_deliverable_id") != final[
                    "final_deliverable_id"
                ]:
                    continue
                decision_event = db.one(
                    "SELECT payload FROM audit_events WHERE run_id = ? "
                    "AND aggregate_id = ? AND event_type IN "
                    "('ApprovalApproved','ApprovalRejected') "
                    "ORDER BY sequence_no DESC LIMIT 1",
                    (service.run_id, release_approval["approval_id"]),
                )
                decision_payload = (
                    _decode_json(decision_event["payload"])
                    if decision_event
                    else {}
                )
                final["release_review"] = {
                    "approval_id": release_approval["approval_id"],
                    "status": release_approval["status"],
                    "comment": decision_payload.get("comment", ""),
                    "decided_sim_time": release_approval["decided_sim_time"],
                }
                break
        lineage = [
            dict(row)
            for row in db.all(
                "SELECT * FROM final_field_lineage WHERE final_deliverable_id = ? "
                "ORDER BY field_path",
                (episode["current_final_deliverable_id"],),
            )
        ]
    processing_job_row = db.one(
        "SELECT * FROM outbox_entries WHERE episode_id = ? "
        "AND effect_type = 'FINAL_ORGANIZATION' "
        "ORDER BY created_sim_time DESC, outbox_id DESC LIMIT 1",
        (service.episode_id,),
    )
    processing_job = dict(processing_job_row) if processing_job_row else None
    if processing_job:
        processing_job["payload"] = _decode_json(processing_job["payload"])
        processing_event = db.one(
            "SELECT event_type, payload, sim_time FROM audit_events "
            "WHERE run_id = ? AND aggregate_id = ? "
            "ORDER BY sequence_no DESC LIMIT 1",
            (service.run_id, processing_job["outbox_id"]),
        )
        processing_job["latest_event"] = (
            {
                "event_type": processing_event["event_type"],
                "payload": _decode_json(processing_event["payload"]),
                "sim_time": processing_event["sim_time"],
            }
            if processing_event
            else None
        )
    participant_rows = db.all(
        "SELECT ep.actor_id, ep.role, a.display_name FROM episode_participants ep "
        "JOIN actors a ON a.actor_id = ep.actor_id "
        "WHERE ep.episode_id = ? "
        "AND ep.role IN ('COORDINATOR','AGGREGATOR','PARTICIPANT','ACTION_OWNER') "
        "AND a.status = 'ACTIVE' ORDER BY a.display_name, ep.actor_id",
        (service.episode_id,),
    )
    participants_by_id: dict[str, dict[str, Any]] = {}
    for participant in participant_rows:
        item = participants_by_id.setdefault(
            participant["actor_id"],
            {
                "actor_id": participant["actor_id"],
                "display_name": participant["display_name"],
                "roles": [],
            },
        )
        item["roles"].append(participant["role"])
    if principal is None:
        memory_rows = db.all(
            "SELECT * FROM collaboration_memories ORDER BY created_sim_time, memory_id"
        )
    elif principal.is_participant:
        memory_rows = db.all(
            "SELECT * FROM collaboration_memories WHERE actor_id = ? "
            "ORDER BY created_sim_time, memory_id",
            (principal.actor_id,),
        )
    else:
        memory_rows = []
    memories = []
    for memory in memory_rows:
        item = dict(memory)
        item["value"] = _decode_json(memory["value"])
        item["evidence_refs"] = _decode_json(memory["evidence_refs"])
        memories.append(item)
    progress_tasks = [task for task in tasks if task["status"] != "REJECTED"]
    meeting_progress = {
        "total": len(progress_tasks),
        "pending_assignment": sum(
            task["status"] == "PENDING_ASSIGNMENT" for task in progress_tasks
        ),
        "needs_revision": sum(
            task["status"] == "NEEDS_REVISION" for task in progress_tasks
        ),
        "tracking": sum(task["status"] == "TRACKING" for task in progress_tasks),
        "pending_acceptance": sum(
            task["status"] == "PENDING_ACCEPTANCE" for task in progress_tasks
        ),
        "completed": sum(
            task["status"] in {"ACCEPTED", "AGGREGATED", "ARCHIVED"}
            for task in progress_tasks
        ),
    }
    # Notices addressed to whoever is looking.
    #
    # The bell used to be derived entirely from `tasks`, which meant it could
    # only ever show things that ask the reader for a decision. A task whose
    # description changed under someone asks for nothing and still has to
    # reach them, so it is read off the Outbox -- the same row Feishu delivers,
    # rather than a second channel that could disagree with it.
    notices: list[dict[str, Any]] = []
    if principal is not None:
        for row in db.all(
            "SELECT outbox_id, effect_id, effect_type, action_item_id, payload, "
            "created_sim_time FROM outbox_entries WHERE episode_id = ? "
            "AND effect_type IN "
            f"({','.join('?' * len(NOTIFICATION_EFFECT_TYPES))}) "
            "ORDER BY created_sim_time DESC, outbox_id DESC LIMIT 60",
            (episode["episode_id"], *sorted(NOTIFICATION_EFFECT_TYPES)),
        ):
            payload = _decode_json(row["payload"]) or {}
            if principal.actor_id not in (payload.get("recipient_actor_ids") or []):
                continue
            notification = payload.get("notification") or {}
            notices.append(
                {
                    "notice_id": row["effect_id"],
                    "kind": row["effect_type"],
                    "action_item_id": row["action_item_id"],
                    "title": notification.get("title", ""),
                    "summary": notification.get("summary", ""),
                    "fields": notification.get("fields", []),
                    "sim_time": row["created_sim_time"],
                    # A notice offering a decision is already surfaced by the
                    # task list; flagged so the bell does not show it twice.
                    "decides": bool(notification.get("decisions")),
                }
            )
            if len(notices) >= 12:
                break

    state = {
        "episode": dict(episode),
        "tasks": tasks,
        "notices": notices,
        "pending_approvals": approvals,
        "final": final,
        "lineage": lineage,
        "result_processing": {
            "automatic": result_processing_mode != "disabled",
            "mode": result_processing_mode,
            "job": processing_job,
        },
        "report": build_report(
            db,
            service.fixture,
            episode_id=service.episode_id,
            run_id=service.run_id,
        ),
        "aggregator_actor_id": service.aggregator_actor_id,
        "participants": list(participants_by_id.values()),
        "memories": memories,
        "memory_lexicon": memory_lexicon_payload(),
        "meeting_progress": meeting_progress,
        "timeline": timeline,
        "agent_trace": _agent_trace(service),
    }
    return _project_workbench_state(state, principal)


SUMMARY_UPSTREAM_FIELDS = (
    "source_ref",
    "action_item_id",
    "title",
    "accepted_version_id",
    "submitted_by_actor_id",
    "submitted_by_display_name",
    "submission_summary",
    "responsibility",
)


def _summarize_collaboration_inputs(
    inputs: dict[str, Any] | None,
) -> dict[str, Any] | None:
    """Keep who delivered and in what direction; drop the delivery body."""

    if not inputs:
        return None
    return {
        "upstream_results": [
            {
                **{
                    key: result.get(key)
                    for key in SUMMARY_UPSTREAM_FIELDS
                    if key in result
                },
                "detail_level": "SUMMARY",
            }
            for result in inputs.get("upstream_results") or []
        ],
        "collective_decision": inputs.get("collective_decision"),
    }


def _version_summary(version: dict[str, Any] | None) -> dict[str, Any] | None:
    if not version:
        return None
    return {
        key: version.get(key)
        for key in (
            "version_id",
            "received_sim_time",
            "validation_status",
            "review_status",
        )
    }


def _project_workbench_state(
    state: dict[str, Any], principal: Principal | None
) -> dict[str, Any]:
    """Apply field-level access rules before a workbench payload leaves the server."""
    if principal is None:
        # Internal tests and diagnostics may request the trusted full projection.
        return state
    projected = deepcopy(state)
    projected["principal"] = {
        "actor_id": principal.actor_id,
        "roles": sorted(role.value for role in principal.roles),
        "auth_source": principal.auth_source,
    }
    if principal.is_coordinator:
        projected["allowed_surfaces"] = ["tasks", "manage", "diagnostics"]
        for task in projected["tasks"]:
            task.pop("collaboration_hints", None)
            current_assignments = task.get("current_assignments", [])
            task["my_assignment"] = next(
                (
                    assignment
                    for assignment in current_assignments
                    if assignment.get("actor_id") == principal.actor_id
                ),
                None,
            )
            task["is_mine"] = task.get("owner_actor_id") == principal.actor_id
            task["is_collaborator"] = principal.actor_id in task.get(
                "active_collaborator_actor_ids", []
            )
            task["has_collaborated"] = principal.actor_id in task.get(
                "historical_collaborator_actor_ids", []
            )
            task["can_contribute"] = bool(
                task["is_mine"] or task["is_collaborator"]
            )
        return projected

    projected["allowed_surfaces"] = ["tasks"]
    visible_tasks: list[dict[str, Any]] = []
    for task in projected["tasks"]:
        if not task.get("published_sim_time"):
            continue
        current_assignments = task.get("current_assignments", [])
        my_assignment = next(
            (
                assignment
                for assignment in current_assignments
                if assignment.get("actor_id") == principal.actor_id
            ),
            None,
        )
        is_mine = task.get("owner_actor_id") == principal.actor_id
        is_collaborator = principal.actor_id in task.get(
            "active_collaborator_actor_ids", []
        )
        has_collaborated = principal.actor_id in task.get(
            "historical_collaborator_actor_ids", []
        )
        can_contribute = bool(is_mine or is_collaborator)
        collaboration = task.get("collaboration_progress")
        is_required_participant = bool(
            collaboration
            and any(
                contribution.get("actor_id") == principal.actor_id
                for contribution in collaboration.get("contributions") or []
            )
        )
        assistance_requests = task.get("assistance_requests") or []
        is_help_target = any(
            request.get("target_actor_id") == principal.actor_id
            and request.get("status") in ("OPEN", "ACKNOWLEDGED")
            for request in assistance_requests
        )
        if not (
            my_assignment
            or is_mine
            or is_collaborator
            or has_collaborated
            or is_required_participant
            or is_help_target
        ):
            continue
        task["my_assignment"] = my_assignment
        task["is_mine"] = is_mine
        task["is_collaborator"] = is_collaborator
        task["has_collaborated"] = has_collaborated
        task["can_contribute"] = can_contribute
        task["collaboration_hints"] = [
            hint
            for hint in task.get("collaboration_hints", [])
            if hint.get("actor_id") != principal.actor_id
        ]
        if collaboration:
            for contribution in collaboration.get("contributions") or []:
                if (
                    contribution.get("contribution_type") == "VOTE"
                    and contribution.get("actor_id") != principal.actor_id
                ):
                    contribution["payload"] = None
                    contribution["payload_ref"] = None
                if (
                    contribution.get("contribution_type") == "BALLOT"
                    and contribution.get("status") != "SUBMITTED"
                    and contribution.get("actor_id") != principal.actor_id
                ):
                    contribution["payload"] = None
                    contribution["payload_ref"] = None
            structure = (task.get("proposal_metadata") or {}).get(
                "collaboration_structure"
            ) or {}
            # The confirmed final owner has to read what colleagues delivered in
            # order to organize it; everyone else in the structure only needs to
            # know who delivered and in what direction.
            if structure.get("required_owner_actor_id") != principal.actor_id:
                task["collaboration_inputs"] = (
                    _summarize_collaboration_inputs(
                        task.get("collaboration_inputs")
                    )
                    if is_required_participant
                    else None
                )
        metadata = task.get("proposal_metadata") or {}
        task["proposal_metadata"] = {
            key: metadata[key]
            for key in (
                "source_timestamp",
                "source_quote",
                "deliverable",
                "work_requirements",
                "priority",
                "collaboration_mode",
                "collaborator_names",
                "collaboration_structure",
            )
            if key in metadata
        }
        task["is_help_target"] = is_help_target
        sanitized_assignments = []
        for assignment in current_assignments:
            item = dict(assignment)
            if assignment.get("actor_id") != principal.actor_id:
                item["response_message"] = None
            sanitized_assignments.append(item)
        task["assignments"] = sanitized_assignments
        task["current_assignments"] = sanitized_assignments
        if not is_mine:
            task["promised_by_sim_time"] = None
            task["active_commitment"] = None
            task["latest_progress"] = None
            task["last_owner_signal"] = None
            visible_activity = []
            for activity in task.get("activity") or []:
                item = dict(activity)
                other_actor = item.get("actor_id") not in {
                    None,
                    principal.actor_id,
                }
                if other_actor and item.get("kind") in {
                    "COMMITMENT",
                    "PROGRESS",
                    "STATUS",
                    "DELIVERY",
                    "CONTRIBUTION",
                    "INTERVENTION",
                }:
                    continue
                if other_actor and item.get("kind") == "ASSIGNMENT":
                    item["detail"] = "同任务成员的派发回应已更新"
                visible_activity.append(item)
            task["activity"] = visible_activity
        if not can_contribute:
            task["active_commitment"] = None
            task["latest_version"] = _version_summary(task.get("latest_version"))
            task["current_version"] = _version_summary(task.get("current_version"))
            task["current_valid_version_id"] = None
            task["accepted_task_result"] = None
            task["latest_progress"] = None
            if not has_collaborated:
                task["activity"] = []
                task["contribution_versions"] = []
            else:
                task["contribution_versions"] = [
                    version
                    for version in task.get("contribution_versions", [])
                    if version.get("submitted_by_actor_id") == principal.actor_id
                ]
                for version in task["contribution_versions"]:
                    version.pop("processing_result", None)
                    version.pop("processing_metadata", None)
                    version.pop("source_manifest", None)
            if not task["is_help_target"]:
                task["assistance_requests"] = [
                    {
                        "assistance_request_id": request.get(
                            "assistance_request_id"
                        ),
                        "status": request.get("status"),
                        "category": request.get("category"),
                        "target_display_name": request.get(
                            "target_display_name"
                        ),
                        "created_sim_time": request.get("created_sim_time"),
                    }
                    for request in assistance_requests
                    if (
                        request.get("target_actor_id") == principal.actor_id
                        or request.get("requester_actor_id") == principal.actor_id
                    )
                ]
                active = task.get("active_assistance")
                task["active_assistance"] = (
                    {
                        "assistance_request_id": active.get(
                            "assistance_request_id"
                        ),
                        "status": active.get("status"),
                        "category": active.get("category"),
                        "target_display_name": active.get(
                            "target_display_name"
                        ),
                    }
                    if active
                    else None
                )
        else:
            for version_field in ("latest_version", "current_version"):
                version = task.get(version_field)
                if version:
                    version.pop("processing_result", None)
                    version.pop("processing_metadata", None)
                    version.pop("source_manifest", None)
            accepted_result = task.get("accepted_task_result")
            if accepted_result:
                accepted_result.pop("processing_metadata", None)
                accepted_result.pop("source_manifest", None)
            if not is_mine:
                task["contribution_versions"] = [
                    version
                    for version in task.get("contribution_versions", [])
                    if version.get("submitted_by_actor_id") == principal.actor_id
                ]
                for version in task["contribution_versions"]:
                    version.pop("processing_result", None)
                    version.pop("processing_metadata", None)
                    version.pop("source_manifest", None)
        visible_tasks.append(task)
    projected["tasks"] = visible_tasks
    projected["pending_approvals"] = []
    if not projected.get("final") or projected["final"].get("status") != "RELEASED":
        projected["final"] = None
        projected["lineage"] = []
    projected["result_processing"] = {
        "automatic": projected["result_processing"]["automatic"],
        "mode": projected["result_processing"]["mode"],
        "job": None,
    }
    projected["report"] = None
    projected["timeline"] = []
    projected["agent_trace"] = []
    projected.pop("aggregator_actor_id", None)
    return projected


def serve_dashboard(
    service: CoordinationService,
    *,
    host: str = "127.0.0.1",
    port: int = 8765,
    result_processing_mode: str = "local",
) -> None:
    if result_processing_mode not in {"bailian", "local", "disabled"}:
        raise ValueError("invalid result processing mode")
    principal_provider = VirtualSessionPrincipalProvider(
        service.db,
        episode_id=service.episode_id,
        secret=os.environ.get("COLWORK_SESSION_SECRET"),
    )
    authorization = AuthorizationService(service.db, episode_id=service.episode_id)

    approval_path = re.compile(r"^/api/approvals/([^/]+)$")
    action_path = re.compile(
        r"^/api/action-items/([^/]+)/(revise|amend|dispatch|assignment-response|ignore|merge|signal|assistance|personal-commitment|submit|ballot-draft|ballot|vote)$"
    )
    collaboration_structure_path = re.compile(
        r"^/api/collaboration-structures/question-vote$"
    )
    collaboration_structure_revoke_path = re.compile(
        r"^/api/collaboration-structures/question-vote/([^/]+)/revoke$"
    )
    assistance_path = re.compile(
        r"^/api/assistance/([^/]+)/(acknowledge|resolve|cancel)$"
    )
    final_generate_path = re.compile(r"^/api/final/generate$")
    memory_declare_path = re.compile(r"^/api/memories/declare$")
    memory_path = re.compile(
        r"^/api/memories/([^/]+)/(confirm|replace|reject)$"
    )
    artifact_review_path = re.compile(r"^/api/artifact-versions/([^/]+)/review$")
    artifact_contribution_path = re.compile(
        r"^/api/artifact-versions/([^/]+)/contribution$"
    )
    artifact_retry_path = re.compile(
        r"^/api/artifact-versions/([^/]+)/retry-processing$"
    )

    class Handler(BaseHTTPRequestHandler):
        def _read_json_body(self) -> Any:
            """Enforce the raw body ceiling before allocating or decoding it."""

            raw_length = self.headers.get("Content-Length", "0")
            try:
                length = int(raw_length)
            except ValueError as error:
                raise RequestTooLarge("Content-Length is not a number") from error
            if length < 0 or length > MAX_REQUEST_BYTES:
                raise RequestTooLarge(
                    f"请求体超过 {MAX_REQUEST_BYTES // (1024 * 1024)}MB 上限"
                )
            return json.loads(self.rfile.read(length) or b"{}")

        def _json(self, status: int, payload: Any) -> None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _principal(self) -> Principal:
            return principal_provider.resolve_authorization_header(
                self.headers.get("Authorization")
            )

        def _audit_rejection(
            self,
            *,
            event_type: str,
            principal: Principal | None,
            operation: str,
            reason: str,
            actor_hint: str | None = None,
        ) -> None:
            # A denied request must not be allowed to fail the response path just
            # because diagnostic persistence is unavailable.
            try:
                service.record_security_rejection(
                    event_type=event_type,
                    actor_id=(principal.actor_id if principal else actor_hint),
                    operation=operation,
                    reason=reason,
                )
            except Exception:
                return

        def do_GET(self) -> None:  # noqa: N802 - standard library API
            parsed = urlparse(self.path)
            from .static_assets import serves as bundle_serves

            if parsed.path in ("/", "/tasks", "/diagnostics"):
                body = WORKBENCH_HTML.encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            elif bundle_serves(parsed.path):
                from .static_assets import AssetMissing, read_asset

                try:
                    body, content_type = read_asset(parsed.path)
                except AssetMissing:
                    from .static_assets import MISSING_BUNDLE_PAGE

                    body, content_type = MISSING_BUNDLE_PAGE, "text/html; charset=utf-8"
                    self.send_response(503)
                else:
                    self.send_response(200)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            elif parsed.path == "/api/observatory":
                from .observatory import build_observatory

                try:
                    principal = self._principal()
                    authorization.require_coordinator(principal)
                except Exception as error:  # noqa: BLE001 - mirrors /api/state
                    self._json(403, {"message": str(error)})
                    return
                requested = parse_qs(parsed.query)
                self._json(
                    200,
                    build_observatory(
                        service.db,
                        episode_id=requested.get(
                            "episode_id", [service.episode_id]
                        )[0],
                        run_id=requested.get("run_id", [service.run_id])[0],
                    ),
                )
            elif parsed.path == "/api/session/actors":
                self._json(200, {"actors": principal_provider.list_selectable_actors()})
            elif parsed.path == "/api/state":
                principal: Principal | None = None
                try:
                    principal = self._principal()
                    surface = parse_qs(parsed.query).get("surface", ["tasks"])[0]
                    if surface == "manage" or surface == "diagnostics":
                        authorization.require_coordinator(principal)
                    elif surface != "tasks":
                        raise ValueError("unknown workbench surface")
                    projected_state = workbench_state(
                        service,
                        result_processing_mode=result_processing_mode,
                        principal=principal,
                    )
                    if principal.is_participant and not principal.is_coordinator:
                        service.record_restricted_field_projection(
                            actor_id=principal.actor_id,
                            session_id=principal.session_id,
                            surface=surface,
                            hidden_fields=[
                                "management_review_policy",
                                "other_participant_delivery_body",
                                "other_participant_private_memory",
                                "unreleased_final",
                                "global_audit_timeline",
                            ],
                        )
                    self._json(200, projected_state)
                except PrincipalError as error:
                    self._audit_rejection(
                        event_type=(
                            "AuthorizationRejected"
                            if principal
                            else "AuthenticationRejected"
                        ),
                        principal=principal,
                        operation=f"GET {parsed.path}",
                        reason=str(error),
                    )
                    self._json(
                        401,
                        {"error": "UNAUTHENTICATED", "message": str(error)},
                    )
            else:
                self._json(404, {"error": "not_found"})

        def do_POST(self) -> None:  # noqa: N802 - standard library API
            parsed = urlparse(self.path)
            if parsed.path == "/api/session":
                actor_hint: str | None = None
                try:
                    payload = self._read_json_body()
                    actor_hint = str(payload.get("actor_id", "")) or None
                    self._json(
                        200, principal_provider.issue(actor_hint or "")
                    )
                except RequestTooLarge as error:
                    self._json(
                        413,
                        {"error": "PAYLOAD_TOO_LARGE", "message": str(error)},
                    )
                except (PrincipalError, ValueError) as error:
                    self._audit_rejection(
                        event_type="AuthenticationRejected",
                        principal=None,
                        actor_hint=actor_hint,
                        operation=f"POST {parsed.path}",
                        reason=str(error),
                    )
                    self._json(
                        403,
                        {"error": "AUTHORIZATION", "message": str(error)},
                    )
                return
            approval_match = approval_path.match(parsed.path)
            final_generate_match = final_generate_path.match(parsed.path)
            action_match = action_path.match(parsed.path)
            collaboration_structure_match = collaboration_structure_path.match(
                parsed.path
            )
            assistance_match = assistance_path.match(parsed.path)
            memory_match = memory_path.match(parsed.path)
            memory_declare_match = memory_declare_path.match(parsed.path)
            structure_revoke_match = collaboration_structure_revoke_path.match(
                parsed.path
            )
            artifact_match = artifact_review_path.match(parsed.path)
            artifact_contribution_match = artifact_contribution_path.match(
                parsed.path
            )
            artifact_retry_match = artifact_retry_path.match(parsed.path)
            if (
                not approval_match
                and not final_generate_match
                and not action_match
                and not collaboration_structure_match
                and not structure_revoke_match
                and not assistance_match
                and not memory_match
                and not memory_declare_match
                and not artifact_match
                and not artifact_contribution_match
                and not artifact_retry_match
            ):
                self._json(404, {"error": "not_found"})
                return
            principal: Principal | None = None
            try:
                payload = self._read_json_body()
                principal = self._principal()
                if approval_match:
                    authorization.require_coordinator(principal)
                    result = service.decide_approval(
                        approval_match.group(1),
                        actor_id=principal.actor_id,
                        approve=bool(payload.get("approve")),
                        comment=payload.get("comment", ""),
                    )
                    service.dispatch_all(session_id="workbench_dispatcher")
                elif final_generate_match:
                    authorization.require_coordinator(principal)
                    # `aggregate` itself will happily summarise a half-finished
                    # meeting -- `eval` and the CLI rely on that. Asking for a
                    # final from the console means something narrower: that the
                    # work is done. Refusing here keeps that promise without
                    # taking the looser behaviour away from the other callers,
                    # and it is a real refusal rather than a greyed-out button
                    # somebody can POST straight past.
                    outstanding = [
                        dict(row)["title"]
                        for row in service.db.all(
                            "SELECT title FROM action_items WHERE episode_id = ? "
                            "AND required = TRUE AND status NOT IN "
                            "('ACCEPTED','AGGREGATED','ARCHIVED','REJECTED') "
                            "ORDER BY action_item_id",
                            (service.episode_id,),
                        )
                    ]
                    if outstanding:
                        self._json(
                            409,
                            {
                                "error": "TASKS_OUTSTANDING",
                                "message": (
                                    f"还有 {len(outstanding)} 项必需任务没验收完："
                                    + "、".join(outstanding[:3])
                                    + ("…" if len(outstanding) > 3 else "")
                                ),
                                "outstanding": outstanding,
                            },
                        )
                        return
                    result = {
                        "final_deliverable_id": service.aggregate(),
                        "outstanding": [],
                    }
                    service.dispatch_all(session_id="workbench_dispatcher")
                elif collaboration_structure_match:
                    authorization.require_coordinator(principal)
                    result = service.confirm_question_vote_structure(
                        collection_action_item_ids=payload.get(
                            "collection_action_item_ids", []
                        ),
                        decision_action_item_id=payload.get(
                            "decision_action_item_id", ""
                        ),
                        final_owner_actor_id=payload.get(
                            "final_owner_actor_id", ""
                        ),
                        voter_actor_ids=payload.get("voter_actor_ids", []),
                        selection_count=payload.get("selection_count", 8),
                        source_span=payload.get("source_span", ""),
                        actor_id=principal.actor_id,
                        message_id=payload.get("message_id", ""),
                    )
                elif assistance_match:
                    authorization.require_episode(principal)
                    assistance_id, assistance_action = assistance_match.groups()
                    result = service.update_assistance(
                        assistance_id,
                        actor_id=principal.actor_id,
                        action=assistance_action,
                        resolution_summary=payload.get("resolution_summary", ""),
                        message_id=payload.get("message_id", ""),
                    )
                elif structure_revoke_match:
                    authorization.require_coordinator(principal)
                    result = service.revoke_question_vote_structure(
                        structure_revoke_match.group(1),
                        actor_id=principal.actor_id,
                        reason=payload.get("reason", ""),
                        message_id=payload.get("message_id", ""),
                    )
                elif memory_declare_match:
                    authorization.require_participant(principal)
                    result = service.declare_collaboration_memory(
                        actor_id=principal.actor_id,
                        topic=payload.get("topic", ""),
                        code=payload.get("code", ""),
                        message_id=payload.get("message_id", ""),
                    )
                elif memory_match:
                    authorization.require_participant(principal)
                    memory_id, memory_action = memory_match.groups()
                    result = service.decide_collaboration_memory(
                        memory_id,
                        actor_id=principal.actor_id,
                        action=memory_action,
                        replacement_code=payload.get("replacement_code", ""),
                        message_id=payload.get("message_id", ""),
                    )
                elif artifact_retry_match:
                    authorization.require_coordinator(principal)
                    result = service.retry_task_result_processing(
                        artifact_retry_match.group(1),
                        actor_id=principal.actor_id,
                        message_id=payload.get("message_id", ""),
                    )
                elif artifact_contribution_match:
                    authorization.require_participant(principal)
                    result = service.decide_contribution(
                        artifact_contribution_match.group(1),
                        actor_id=principal.actor_id,
                        action=payload.get("action", ""),
                        comment=payload.get("comment", ""),
                        message_id=payload.get("message_id", ""),
                    )
                elif artifact_match:
                    authorization.require_coordinator(principal)
                    result = service.review_artifact(
                        artifact_match.group(1),
                        actor_id=principal.actor_id,
                        approve=bool(payload.get("approve")),
                        comment=payload.get("comment", ""),
                        message_id=payload.get("message_id", ""),
                        completion_report=payload.get("completion_report"),
                    )
                else:
                    action_id, operation = action_match.groups()
                    message_id = payload.get("message_id", "")
                    if operation == "revise":
                        authorization.require_coordinator(principal)
                        result = service.revise_action_proposal(
                            action_id,
                            actor_id=principal.actor_id,
                            title=payload.get("title", ""),
                            deliverable=payload.get("deliverable", ""),
                            acceptance_criteria=payload.get(
                                "acceptance_criteria", ""
                            ),
                            priority=payload.get("priority", "P1"),
                            message_id=message_id,
                            team_required_by_sim_time=payload.get(
                                "team_required_by_sim_time"
                            ),
                            work_requirements=payload.get("work_requirements"),
                            management_review_policy=payload.get(
                                "management_review_policy"
                            ),
                        )
                    elif operation == "amend":
                        # No coordinator check: the service requires the caller
                        # to be the task's own owner, which the coordinator is
                        # not unless the task was dispatched to them.
                        authorization.require_participant(principal)
                        result = service.amend_task_description(
                            action_id,
                            actor_id=principal.actor_id,
                            title=payload.get("title", ""),
                            deliverable=payload.get("deliverable", ""),
                            message_id=message_id,
                        )
                    elif operation == "dispatch":
                        authorization.require_coordinator(principal)
                        result = service.dispatch_action(
                            action_id,
                            actor_id=principal.actor_id,
                            owner_actor_id=payload.get("owner_actor_id", ""),
                            collaborator_actor_ids=payload.get(
                                "collaborator_actor_ids", []
                            ),
                            assignment_message=payload.get(
                                "assignment_message", ""
                            ),
                            message_id=message_id,
                        )
                    elif operation == "assignment-response":
                        authorization.require_participant(principal)
                        result = service.respond_to_assignment(
                            action_id,
                            actor_id=principal.actor_id,
                            decision=payload.get("decision", ""),
                            response_message=payload.get(
                                "response_message", ""
                            ),
                            message_id=message_id,
                        )
                    elif operation == "ignore":
                        authorization.require_coordinator(principal)
                        result = service.ignore_action(
                            action_id,
                            actor_id=principal.actor_id,
                            reason=payload.get("reason", ""),
                            message_id=message_id,
                        )
                    elif operation == "merge":
                        authorization.require_coordinator(principal)
                        result = service.merge_action(
                            action_id,
                            target_action_item_id=payload.get(
                                "target_action_item_id", ""
                            ),
                            actor_id=principal.actor_id,
                            message_id=message_id,
                        )
                    elif operation == "signal":
                        authorization.require_action_contributor(
                            principal, action_id
                        )
                        result = service.record_progress_signal(
                            action_id,
                            actor_id=principal.actor_id,
                            signal_type=payload.get("signal_type", ""),
                            valid_until=payload.get("valid_until"),
                            note=payload.get("note", ""),
                            message_id=message_id,
                        )
                    elif operation == "assistance":
                        authorization.require_action_contributor(
                            principal, action_id
                        )
                        result = service.request_assistance(
                            action_id,
                            actor_id=principal.actor_id,
                            target_actor_id=payload.get("target_actor_id", ""),
                            category=payload.get("category", "OTHER"),
                            summary=payload.get("summary", ""),
                            blocking_action_item_id=payload.get(
                                "blocking_action_item_id"
                            ),
                            message_id=message_id,
                        )
                    elif operation == "personal-commitment":
                        authorization.require_action_owner(principal, action_id)
                        result = service.revise_personal_commitment(
                            action_id,
                            actor_id=principal.actor_id,
                            proposed_deadline_sim_time=payload.get(
                                "proposed_deadline_sim_time", ""
                            ),
                            reason=payload.get("reason", ""),
                            message_id=message_id,
                        )
                    elif operation == "ballot-draft":
                        authorization.require_participant(principal)
                        result = service.prepare_question_ballot_draft(
                            action_id,
                            actor_id=principal.actor_id,
                            processing_mode=(
                                "bailian"
                                if result_processing_mode == "bailian"
                                else "local"
                            ),
                            message_id=message_id,
                        )
                    elif operation == "ballot":
                        authorization.require_participant(principal)
                        result = service.open_question_ballot(
                            action_id,
                            actor_id=principal.actor_id,
                            options=payload.get("options", []),
                            message_id=message_id,
                        )
                    elif operation == "vote":
                        authorization.require_participant(principal)
                        result = service.submit_question_vote(
                            action_id,
                            actor_id=principal.actor_id,
                            scores=payload.get("scores", {}),
                            message_id=message_id,
                        )
                    else:
                        authorization.require_action_contributor(
                            principal, action_id
                        )
                        result = service.submit_artifact(
                            action_id,
                            actor_id=principal.actor_id,
                            message_id=message_id,
                            payload=payload.get("delivery", {}),
                        )
                self._json(200, result)
            except PrincipalError as error:
                self._audit_rejection(
                    event_type=(
                        "AuthorizationRejected"
                        if principal
                        else "AuthenticationRejected"
                    ),
                    principal=principal,
                    operation=f"POST {parsed.path}",
                    reason=str(error),
                )
                self._json(
                    403,
                    {"error": "AUTHORIZATION", "message": str(error)},
                )
            except PermissionError as error:
                self._audit_rejection(
                    event_type="AuthorizationRejected",
                    principal=principal,
                    operation=f"POST {parsed.path}",
                    reason=str(error),
                )
                self._json(
                    403,
                    {"error": "AUTHORIZATION", "message": str(error)},
                )
            except RequestTooLarge as error:
                self._audit_rejection(
                    event_type="RequestRejected",
                    principal=principal,
                    operation=f"POST {parsed.path}",
                    reason=str(error),
                )
                self._json(
                    413,
                    {"error": "PAYLOAD_TOO_LARGE", "message": str(error)},
                )
            except (KeyError, ValueError) as error:
                message = str(error)
                conflict = any(
                    token in message
                    for token in ("already", "claimed", "competing", "published")
                )
                self._json(
                    409 if conflict else 400,
                    {"error": type(error).__name__, "message": message},
                )

        def log_message(self, format: str, *args: Any) -> None:
            return

    try:
        server = SingleInstanceHTTPServer((host, port), Handler)
    except OSError as error:
        raise RuntimeError(
            f"workbench address http://{host}:{port} is already in use; "
            "stop the existing instance before starting another"
        ) from error
    server.timeout = 1.0
    try:
        while True:
            server.handle_request()
    finally:
        server.server_close()
