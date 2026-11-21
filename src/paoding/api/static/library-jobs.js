const trackedJobId = new URLSearchParams(location.search).get('job');
const activeJobStates = new Set(['queued', 'running', 'retrying']);
let trackedJob = null;
let trackedTimer;
let trackedRequest;
let retryInFlight = false;

function jobLink(id) {
  return '/library?job=' + encodeURIComponent(id) + '#recent';
}

function jobTime(value) {
  const time = new Date(value);
  if (!value || Number.isNaN(time.valueOf())) return '';
  return new Intl.DateTimeFormat('zh-CN', {
    month: 'numeric', day: 'numeric', hour: '2-digit', minute: '2-digit'
  }).format(time);
}

let jobsVersion = 0;

async function loadJobs() {
  const version = ++jobsVersion;
  const box = $('jobs');
  try {
    const jobs = await api('/api/v1/jobs?limit=8');
    if (version !== jobsVersion) return;
    box.replaceChildren();
    if (!jobs.length) box.append(el('p', '暂无任务，收录内容后可在这里查看进度。'));
    for (const job of jobs) {
      const row = el('div', null, 'job');
      const type = jobKinds[job.kind] || job.kind;
      const link = el('a', null, 'job-link');
      link.href = jobLink(job.id);
      if (job.id === trackedJobId) link.setAttribute('aria-current', 'true');
      const meta = [job.title ? type : '', jobTime(job.created_at), job.id.slice(0, 8)].filter(Boolean);
      link.append(el('strong', job.title || type), el('small', meta.join(' · ')));
      const state = el('div');
      state.append(el('span', jobStates[job.status] || job.status));
      if (job.status === 'failed' && job.id !== trackedJobId) {
        const retry = el('button', '重试');
        retry.type = 'button';
        retry.onclick = async () => {
          retry.disabled = true;
          try {
            await api('/api/v1/jobs/' + encodeURIComponent(job.id) + '/retry', {method: 'POST'});
            await loadJobs();
          } catch (error) {
            let notice = row.querySelector('[role="alert"]');
            if (!notice) {
              notice = el('span', null, 'alert');
              notice.setAttribute('role', 'alert');
              row.append(notice);
            }
            notice.textContent = error.message;
            retry.disabled = false;
          }
        };
        state.append(document.createTextNode(' '), retry);
      }
      row.append(link, state);
      box.append(row);
    }
  } catch (error) {
    if (version === jobsVersion) box.replaceChildren(el('p', '无法加载任务：' + error.message));
  }
}

function describeJob(job) {
  const result = job.result || {};
  switch (job.status) {
    case 'queued':
      return Date.now() - Date.parse(job.created_at) > 60000
        ? '任务仍在队列中。如果一直在等待，请确认处理窗口已打开。单独启动网页时，请在项目目录另开终端运行 uv run paoding worker。'
        : '已进入队列，正在等待后台处理。此处会自动更新。';
    case 'running':
      return '后台正在处理，关闭网页后仍会继续。';
    case 'retrying':
      return '上次处理暂未成功，后台将自动重试。';
    case 'failed':
      if (job.error?.code === 'configuration_required') {
        return '请检查模型或采集服务配置，保存后可重试这条任务。';
      }
      if (job.error?.code === 'invalid_source') return '请检查原内容是否仍可访问。如需更换链接，可重新收录。';
      return '可以重试这条任务。' + (job.error?.code ? '错误代码 ' + job.error.code : '');
    case 'succeeded':
      if (result.node_id) return '知识卡片已生成，可以直接打开阅读。';
      if (result.account_id) return '本轮巡检已完成，已入队 ' + (result.enqueued_jobs || []).length + ' 条作品。';
      if (result.skipped === 'account_paused') return '该账号已暂停追踪，本次巡检已跳过。';
      return '后台处理已完成。';
    default:
      return '';
  }
}

function showJob(job) {
  const state = jobStates[job.status] || job.status;
  const type = jobKinds[job.kind] || job.kind;
  const status = [state, jobTime(job.created_at), job.id.slice(0, 8)].filter(Boolean).join(' · ');
  $('tracked-title').textContent = job.title ? type + ' · ' + job.title : type;
  if ($('tracked-status').textContent !== status) $('tracked-status').textContent = status;
  $('tracked-hint').textContent = describeJob(job);
  $('tracked-retry').hidden = job.status !== 'failed';
  $('tracked-help').hidden = job.status !== 'failed' || job.error?.code !== 'configuration_required';
  const result = job.status === 'succeeded' && job.result;
  const button = $('tracked-result');
  button.hidden = !(result?.node_id || result?.account_id);
  if (!button.hidden) {
    button.textContent = result.node_id ? '查看知识卡片' : '查看追踪账号';
    button.onclick = result.node_id
      ? () => openDetail(result.node_id, button)
      : () => openCreator({id: result.account_id, name: job.title || '追踪账号'}, button);
  }
}

async function loadTrackedJob() {
  if (!trackedJobId || retryInFlight) return;
  clearTimeout(trackedTimer);
  trackedRequest?.abort();
  const request = new AbortController();
  trackedRequest = request;
  $('tracked-job').hidden = false;
  $('tracked-reload').onclick = loadTrackedJob;
  $('tracked-retry').onclick = retryTrackedJob;
  if (!trackedJob) $('tracked-status').textContent = '正在查询任务…';
  try {
    const job = await api('/api/v1/jobs/' + encodeURIComponent(trackedJobId), {signal: request.signal});
    if (request.signal.aborted) return;
    const finished = activeJobStates.has(trackedJob?.status) && !activeJobStates.has(job.status);
    trackedJob = job;
    $('tracked-reload').hidden = true;
    showJob(job);
    if (finished) {
      loadJobs();
      loadStats();
      loadCards();
      if (job.kind === 'watch') loadExtras();
    }
    if (activeJobStates.has(job.status) && !document.hidden) {
      trackedTimer = setTimeout(loadTrackedJob, 5000);
    }
  } catch (error) {
    if (request.signal.aborted) return;
    $('tracked-status').textContent = '暂时无法查询这条任务';
    $('tracked-hint').textContent = error.status === 404 ? '找不到该任务，请检查链接或从下方任务记录重新选择。' : error.message;
    $('tracked-reload').hidden = false;
  }
}

async function retryTrackedJob() {
  if (retryInFlight) return;
  retryInFlight = true;
  trackedRequest?.abort();
  const button = $('tracked-retry');
  button.disabled = true;
  try {
    await api('/api/v1/jobs/' + encodeURIComponent(trackedJobId) + '/retry', {method: 'POST'});
    loadJobs();
  } catch (error) {
    $('tracked-hint').textContent = error.message;
    $('tracked-reload').hidden = false;
    return;
  } finally {
    button.disabled = false;
    retryInFlight = false;
  }
  await loadTrackedJob();
}

document.addEventListener('visibilitychange', () => {
  clearTimeout(trackedTimer);
  if (!document.hidden && (!trackedJob || activeJobStates.has(trackedJob.status))) loadTrackedJob();
});
window.addEventListener('pagehide', () => {
  clearTimeout(trackedTimer);
  trackedRequest?.abort();
});
window.addEventListener('pageshow', event => {
  if (event.persisted) loadTrackedJob();
});
