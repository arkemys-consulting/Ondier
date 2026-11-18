'use strict';
const main = document.querySelector('#main');
const videoPage = document.querySelector('#videoPage');
const skipLink = document.querySelector('.skip-link');
const dialog = document.querySelector('#dialog');
const dc = document.querySelector('#dialogContent');
const player = document.querySelector('#player');
const audio = document.querySelector('#audio');
const nav = document.querySelector('#libraryNav');
const toolbar = document.querySelector('#toolbar');
const archiveInput = document.querySelector('#archiveTerm');
const playPause = document.querySelector('#playPause');
const playIcon = document.querySelector('#playIcon');
const elapsed = document.querySelector('#elapsed');
const duration = document.querySelector('#duration');
const seek = document.querySelector('#seek');
const playbackStatus = document.querySelector('#playbackStatus');
const nowArt = document.querySelector('#nowArt');
const nowShow = document.querySelector('#nowShow');
let signedIn = false, scrubbing = false;
const defaultSearchScope = 'apple';
const searchState = {scope:defaultSearchScope, query:''};
let subscriptionFilter = '';
const searchScopes = {
  apple: {label:'Apple Podcasts', placeholder:'Search by podcast name or publisher'},
  youtube: {label:'YouTube', placeholder:'Search by channel name or @handle'},
  episodes: {label:'Episodes', placeholder:'Search titles, descriptions or transcripts'}
};
let shows = [], progress = {}, playing = null, last = 0, request = 0;
let videoSession = null, audioReady = false;
let playingShow = null, chapters = [], sleepTimer = null, sleepAt = null, sleepStopped = false;
let notificationCursor = Date.now() / 1000;
const node = (tag, text = '', className = '') => {
  const element = document.createElement(tag); element.textContent = text; element.className = className; return element;
};
function button(text, action, className = '') {
  const element = node('button', text, className); element.type = 'button'; element.addEventListener('click', action); return element;
}
function anchor(text, url, className = '') {
  const element = node('a', text, className);
  if (new URL(url, location.href).protocol === 'https:' || new URL(url, location.href).protocol === 'http:') element.href = url;
  return element;
}
function cover(url) {
  const image = document.querySelector('#cover').content.firstElementChild.cloneNode();
  if (url && url.startsWith('https://')) image.src = url;
  image.addEventListener('error', () => {image.src = '/logo.svg';}, {once:true}); return image;
}
async function api(path, body, method = body === undefined ? 'GET' : 'POST') {
  const response = await fetch(path, {method, headers: body === undefined ? {} : {'Content-Type':'application/json'}, body: JSON.stringify(body)});
  const data = await response.json();
  if (!response.ok) {
    if (response.status === 401 && signedIn) {
      resetSession();
      login(false);
    }
    throw Error(data.error || `HTTP ${response.status}`);
  }
  return data;
}
function status(text) { const p = node('p', text, 'status'); p.setAttribute('role', 'status'); return p; }
function showError(error) { main.append(status(error.message)); }
function ordered(items) { return [...items].sort((a,b) => a.title.localeCompare(b.title)); }
function icon(name) {
  const symbol = node('span', '', `icon icon-${name}`);
  symbol.setAttribute('aria-hidden','true');
  return symbol;
}
function iconButton(name, label, action, title = label, appearance = '') {
  const control = button('',action,`round-button ${appearance}`);
  control.append(icon(name));
  control.setAttribute('aria-label', label);
  control.title = title;
  return control;
}
function setPage(id, title) {
  closeVideo();
  for (const item of nav.querySelectorAll('button')) {
    if (item.id === id) item.setAttribute('aria-current','page');
    else item.removeAttribute('aria-current');
  }
  document.querySelector('#pageTitle').textContent = title;
  document.body.classList.toggle('is-searching',id === 'search');
  if (id !== 'search') archiveInput.value = '';
}
function setSignedIn(value) {
  signedIn = value;
  toolbar.hidden = !value;
  nav.hidden = !value;
  document.body.classList.toggle('is-signed-out', !value);
}
function searchField(placeholder) {
  const input = node('input');
  input.type = 'search';
  input.placeholder = placeholder;
  input.setAttribute('aria-label', placeholder);
  return input;
}
async function library() {
  const id = ++request;
  setPage('library','Library');
  main.replaceChildren(status('Loading library…'));
  try {
    const data = await api('/api/library');
    if (id !== request) return;
    shows = ordered(data.shows);
    progress = data.progress;
    const input = searchField('Filter Shows');
    const header = node('div', '', 'heading');
    header.append(node('h2','All Shows'),input);
    const grid = node('div','','grid');
    const draw = () => {
      const query = input.value.toLocaleLowerCase().trim();
      const items = shows.filter(show => [show.title,show.publisher,...(show.hosts || []).map(host => host.name)]
        .join(' ').toLocaleLowerCase().includes(query));
      grid.replaceChildren(...items.map(show => {
        const tile = button('', () => detail(show), 'tile');
        tile.append(cover(show.artwork), node('strong',show.title));
        if (show.publisher) tile.append(node('small',show.publisher));
        return tile;
      }));
      if (!items.length) grid.append(status(query ? 'No matching shows.' : 'Add a podcast to start your library.'));
    };
    input.addEventListener('input', draw);
    main.replaceChildren(node('h1','Library','visually-hidden'),header,grid);
    draw();
    api('/api/continue').then(data => {
      if (id === request && data.episodes.length) header.before(continueSection(data.episodes));
    }).catch(()=>{});
  } catch (error) {
    if (id === request) main.replaceChildren(status(error.message),button('Retry',library));
  }
}
async function subscriptions() {
  const page = ++request;
  setPage('subscriptions','Subscriptions');
  main.replaceChildren(status('Loading subscriptions…'));
  try {
    const data = await api('/api/library');
    if (page !== request) return;
    shows = ordered(data.shows);
    progress = data.progress;

    const layout = node('section','','subscriptions-page');
    const header = node('div','','heading');
    const actions = node('div','','actions');
    const exportOPML = anchor('Export OPML','/library.opml','quiet');
    exportOPML.download = 'ondier.opml';
    actions.append(button('Import OPML',() => opmlImport(subscriptions)),
      exportOPML,button('Add Podcast',() => discover(true),'add'));
    header.append(node('h1','Subscriptions'),actions);

    const toolbar = node('div','','subscriptions-filter');
    const filter = searchField('Filter subscriptions');
    filter.value = subscriptionFilter;
    const count = status('');
    toolbar.append(filter,count);
    const rows = node('ul','','rows subscription-rows');
    const empty = node('p','','status');
    layout.append(header,toolbar,rows,empty);
    main.replaceChildren(layout);

    const entries = shows.map(show => ({show, host:new URL(show.url).hostname.replace(/^www\./,'')}));
    function draw() {
      subscriptionFilter = filter.value;
      const term = filter.value.trim().toLocaleLowerCase();
      const matches = entries.filter(({show,host}) => `${show.title} ${show.publisher} ${host}`.toLocaleLowerCase().includes(term));
      count.textContent = term
        ? `${matches.length} of ${entries.length} subscriptions`
        : `${entries.length} ${entries.length === 1 ? 'subscription' : 'subscriptions'}`;
      rows.replaceChildren(...matches.map(({show,host}) => {
        const row = node('li','','row');
        const identity = button('',() => detail(show,false,true),'result subscription-title');
        const image = cover(show.artwork);
        image.classList.add('mini-cover');
        const label = node('span');
        label.append(node('strong',show.title),node('small',[show.publisher,host].filter(Boolean).join(' · ')));
        identity.append(image,label);
        const manage = button('Manage',() => settings(show,{onRemove:subscriptions}));
        manage.setAttribute('aria-label',`Manage ${show.title}`);
        row.append(identity,manage);
        return row;
      }));
      empty.textContent = matches.length ? '' : term ? 'No matching subscriptions.' : 'No subscriptions yet.';
    }
    filter.addEventListener('input',draw);
    draw();
  } catch (error) {
    if (page === request) main.replaceChildren(status(error.message),button('Retry',subscriptions));
  }
}
function continueSection(episodes) {
  const [{episode, show, progress: position}, ...others] = episodes;
  const section = node('section','','continue');
  const header = node('div','','heading');
  header.append(node('h2','Continue Listening'),button('Manage',() => {
    const form = node('form','','player-option-list');
    const note = status('');
    for (const entry of episodes) {
      const input = node('input'); input.type = 'checkbox'; input.value = entry.episode.id;
      const label = node('label','','check-option'); label.append(input,node('span',entry.episode.title));
      form.append(label);
    }
    const reset = node('button','Mark Selected as Unplayed');
    reset.disabled = true;
    form.addEventListener('change',() => { reset.disabled = !form.querySelector('input:checked'); });
    form.addEventListener('submit',async event => {
      event.preventDefault(); reset.disabled = true;
      const ids = Array.from(form.querySelectorAll('input:checked'),input => input.value);
      try {
        await api('/api/listening',{operation:'reset',ids},'PUT');
        dialog.close(); await library();
      } catch(error) { note.textContent = error.message; reset.disabled = false; }
    });
    form.append(reset,note);
    openDialog(node('h2','Continue Listening'),form);
  }));
  section.append(header);
  const feature = node('div','','continue-feature');
  const backdrop = cover(show.artwork); backdrop.classList.add('feature-backdrop');
  const art = cover(show.artwork); art.classList.add('feature-cover'); art.loading = 'eager';
  const summary = node('div','','feature-copy');
  summary.append(node('small',show.title), node('h3',episode.title,'feature-title'));
  if (episode.duration > position.position) {
    summary.append(node('p',`${formatDuration(episode.duration-position.position)} left`,'feature-time'));
  }
  const resume = button('Resume', () => play(episode,show), 'primary resume-button');
  resume.prepend(icon('play'));
  resume.setAttribute('aria-label', `Resume ${episode.title}`);
  summary.append(resume);
  feature.append(backdrop,art,summary);
  section.append(feature);
  if (others.length) {
    const list = node('ul','','rows continue-queue');
    for (const entry of others.slice(0,4)) {
      const row = node('li','','row');
      const info = node('div','','result');
      const artwork = cover(entry.show.artwork); artwork.classList.add('mini-cover');
      const copy = node('div');
      const remaining = entry.episode.duration - entry.progress.position;
      copy.append(node('strong',entry.episode.title),
        node('small',entry.show.title + (remaining > 0 ? ` · ${formatDuration(remaining)} left` : '')));
      info.append(artwork,copy);
      row.append(info, iconButton('play',`Resume ${entry.episode.title}`, () => play(entry.episode,entry.show),'Resume'));
      list.append(row);
    }
    section.append(list);
  }
  return section;
}
function formatDuration(seconds) {
  if (!Number.isFinite(seconds) || seconds <= 0) return '';
  return seconds < 60 ? '< 1 min' : `${Math.floor(seconds / 60)} min`;
}
function clock(seconds) {
  const value = Number.isFinite(seconds) ? Math.max(0, Math.floor(seconds)) : 0;
  const minutes = Math.floor(value / 60);
  const tail = `${minutes % 60}:${String(value % 60).padStart(2,'0')}`;
  return minutes >= 60 ? `${Math.floor(minutes / 60)}:${tail.padStart(5,'0')}` : tail;
}
function audioLength() {
  if (Number.isFinite(audio.duration) && audio.duration > 0) return audio.duration;
  return playing?.duration > 0 ? playing.duration : 0;
}
function updatePlayer() {
  const paused = audio.paused || audio.ended;
  const label = paused ? 'Play' : 'Pause';
  playIcon.className = `icon icon-${paused ? 'play' : 'pause'}`;
  playPause.setAttribute('aria-label',label);
  playPause.title = label;
  const length = audioLength();
  duration.textContent = clock(length);
  seek.max = Math.ceil(length);
  seek.disabled = !audioReady || length <= 0;
  if (!scrubbing) {
    seek.value = audioReady ? audio.currentTime : 0;
    elapsed.textContent = clock(seek.valueAsNumber);
  }
  seek.setAttribute('aria-valuetext',`${elapsed.textContent} of ${duration.textContent}`);
}
function moveAudio(seconds) {
  if (!audioReady || audioLength() <= 0) return;
  audio.currentTime = Math.max(0,Math.min(audioLength(),seconds));
}
function stopPlayer(save = true) {
  if (save) savePlayback();
  audioReady = false;
  playing = null;
  playingShow = null;
  chapters = [];
  clearSleepTimer();
  sleepStopped = false;
  scrubbing = false;
  audio.pause();
  audio.removeAttribute('src');
  audio.load();
  player.hidden = true;
  document.body.classList.remove('has-player');
}
async function resumeAudio() {
  try {
    sleepStopped = false;
    await audio.play();
  } catch {
    const message = 'Playback could not start. Try again in a moment.';
    playbackStatus.textContent = message;
    nowShow.textContent = message;
  }
}
playPause.addEventListener('click', () => audio.paused || audio.ended ? resumeAudio() : audio.pause());
document.querySelector('#backward').addEventListener('click', () => moveAudio(audio.currentTime - 15));
document.querySelector('#forward').addEventListener('click', () => moveAudio(audio.currentTime + 30));
const rateControl = document.querySelector('#rate');
let rateRevision = 0;
rateControl.addEventListener('change', async event => {
  const rate = Number(event.target.value);
  ++rateRevision;
  audio.playbackRate = rate;
  try { await api('/api/listening',{operation:'speed',rate,updatedAt:Date.now()/1000},'PUT'); }
  catch(error) { playbackStatus.textContent = error.message; }
});
document.querySelector('#volume').addEventListener('input', event => {audio.volume = Number(event.target.value);});
document.querySelector('#closePlayer').addEventListener('click', () => {stopPlayer();main.focus();});
seek.addEventListener('input', () => {
  scrubbing = true;
  elapsed.textContent = clock(seek.valueAsNumber);
  seek.setAttribute('aria-valuetext',`${elapsed.textContent} of ${duration.textContent}`);
});
seek.addEventListener('change', () => {
  scrubbing = false;
  moveAudio(seek.valueAsNumber);
  updatePlayer();
});
for (const event of ['play','pause','ended','durationchange','timeupdate','loadedmetadata','emptied']) {
  audio.addEventListener(event,updatePlayer);
}
audio.addEventListener('play', () => {
  if (videoSession) audio.pause();
  nowShow.textContent = nowShow.dataset.title ?? '';
});
nowArt.addEventListener('error', () => {
  if (!nowArt.src.endsWith('/logo.svg')) nowArt.src = '/logo.svg';
});
function savePlayback() {
  if (!playing || !audioReady || !Number.isFinite(audio.currentTime) || !audio.currentSrc) return;
  const position = {position: audio.currentTime, finished: audio.ended, updatedAt: Date.now() / 1000};
  progress[playing.id] = position;
  return fetch('/api/progress',{method:'PUT',keepalive:true,headers:{'Content-Type':'application/json'},body:JSON.stringify({id:playing.id,progress:position})})
    .catch(() => {});
}
audio.addEventListener('timeupdate', () => { if (!audio.paused && Date.now()-last >= 12000) {last=Date.now();savePlayback();} });
for (const name of ['pause','ended','seeked']) audio.addEventListener(name, savePlayback);
window.addEventListener('pagehide',savePlayback);
audio.addEventListener('timeupdate', () => {
  if (sleepAt !== null && audio.currentTime >= sleepAt) {
    sleepStopped = true;
    audio.pause();
    clearSleepTimer();
  }
});
audio.addEventListener('ended', async () => {
  const finished = playing;
  if (sleepStopped) return;
  if (sleepAt !== null) { clearSleepTimer(); return; }
  try {
    const {episode:next} = await api('/api/queue',{},'PUT');
    if (next) {
      if (playing === finished && audio.ended) await play(next.episode,next.show);
      else await api('/api/listening',{operation:'next',id:next.episode.id},'PUT');
    }
  } catch (error) { playbackStatus.textContent = error.message; }
});
async function play(ep, show) {
  if (playing?.id === ep.id) { await resumeAudio(); return; }
  if (playing) savePlayback();
  audioReady = false;
  audio.pause();
  audio.removeAttribute('src');
  audio.load();
  playing = ep;
  sleepStopped = false;
  playingShow = show;
  chapters = ep.chapters || [];
  if (sleepAt !== null) clearSleepTimer();
  player.hidden = false;
  scrubbing = false;
  document.body.classList.add('has-player');
  nowArt.src = show.artwork?.startsWith('https://') ? show.artwork : '/logo.svg';
  document.querySelector('#now').textContent = ep.title;
  nowShow.dataset.title = show.title;
  nowShow.textContent = show.title;
  playbackStatus.textContent = '';
  updatePlayer();
  api(`/api/chapters?show=${show.id}&episode=${ep.id}`).then(data => {
    if (playing === ep) chapters = data.chapters.sort((a,b)=>a.startTime-b.startTime);
  }).catch(()=>{});
  api('/api/listening',{operation:'remove',id:ep.id},'PUT').catch(error => {
    if (playing === ep) playbackStatus.textContent = error.message;
  });
  const revision = rateRevision;
  api('/api/listening').then(prefs => {
    if (playing !== ep || revision !== rateRevision || !(prefs.speed >= .75 && prefs.speed <= 2)) return;
    const value = String(prefs.speed);
    if (!Array.from(rateControl.options).some(option => option.value === value)) {
      const option = node('option',value+'×'); option.value = value; rateControl.append(option);
    }
    rateControl.value = value;
    audio.playbackRate = prefs.speed;
  }).catch(()=>{});
  try {
    const latest=await api('/api/progress'); if (playing?.id !== ep.id) return;
    const remote=latest.progress[ep.id];
    if(remote && remote.updatedAt>(progress[ep.id]?.updatedAt??0))progress[ep.id]=remote;
  } catch {}
  if (playing?.id !== ep.id) return;
  const resume = progress[ep.id];
  audio.onloadedmetadata = () => {
    if (resume && !resume.finished && resume.position < audio.duration) audio.currentTime=resume.position;
    audioReady = true;
    updatePlayer();
  };
  audio.src = ep.audio;
  audio.playbackRate = Number(rateControl.value);
  await resumeAudio();
}

function clearSleepTimer() {
  if (sleepTimer) window.clearTimeout(sleepTimer.id);
  sleepTimer = null;
  sleepAt = null;
}

function playbackOptions() {
  if (!playingShow) return;
  const menu = node('div','','player-option-list');
  menu.append(button('Go to Show',() => { dialog.close(); detail(playingShow); }));
  const timer = node('select');
  const length = audioLength();
  for (const [value,label] of [['off','Off'],['15','15 Minutes'],['30','30 Minutes'],['45','45 Minutes'],['60','60 Minutes'],['chapter','End of Chapter']]) {
    if (value === 'chapter' && (!chapters.length || !audioReady || !length)) continue;
    const option = node('option',label);
    option.value = value;
    timer.append(option);
  }
  timer.value = sleepAt !== null ? 'chapter' : sleepTimer ? String(sleepTimer.choice) : 'off';
  timer.addEventListener('change',() => {
    clearSleepTimer();
    if (timer.value === 'chapter') {
      sleepAt = chapters.find(chapter => chapter.startTime > audio.currentTime)?.startTime ?? length;
    } else if (timer.value !== 'off') {
      const choice = Number(timer.value);
      sleepTimer = {choice, id: window.setTimeout(() => { audio.pause(); clearSleepTimer(); }, choice * 60000)};
    }
  });
  menu.append(field('Sleep Timer',timer));
  if (chapters.length) {
    const list = node('div','','player-option-list');
    list.append(node('h3','Chapters'));
    for (const chapter of chapters) {
      list.append(button(`${clock(chapter.startTime)} · ${chapter.title}`,() => {
        moveAudio(chapter.startTime);
        dialog.close();
      }));
    }
    menu.append(list);
  }
  if (typeof audio.webkitShowPlaybackTargetPicker === 'function' || audio.remote?.prompt) {
    const note = status('');
    menu.append(button(typeof audio.webkitShowPlaybackTargetPicker === 'function' ? 'AirPlay' : 'Audio Output',async () => {
      try {
        if (audio.webkitShowPlaybackTargetPicker) audio.webkitShowPlaybackTargetPicker();
        else await audio.remote.prompt();
      } catch(error) { if (error.name !== 'NotAllowedError') note.textContent = error.message; }
    }),note);
  }
  openDialog(node('h2','Playback Options'),menu);
}
function closeVideo(restoreOrigin = false) {
  if (!videoSession) return;
  saveVideoPosition(videoSession);
  const {video,hls,trigger,scroll} = videoSession;
  videoSession = null;
  video.pause();
  hls?.destroy();
  video.removeAttribute('src');
  video.load();
  videoPage.hidden = true;
  videoPage.replaceChildren();
  document.body.classList.remove('is-watching');
  main.hidden = false;
  skipLink.hash = main.id;
  if (restoreOrigin) {
    (trigger?.isConnected ? trigger : main).focus({preventScroll:true});
    window.scrollTo(0,scroll);
  }
}

function videoUpdate(session, update, keepalive = false) {
  if (!session.identifier) return;
  return fetch('/api/video-playback/' + encodeURIComponent(session.identifier), {
    method:'PUT', keepalive, headers:{'Content-Type':'application/json'},
    body:JSON.stringify({...update,updatedAt:Date.now()/1000})
  }).catch(() => {});
}

function saveVideoPosition(session, keepalive = false) {
  if (!session.ready || !Number.isFinite(session.video.currentTime)) return;
  return videoUpdate(session,{position:session.video.ended ? 0 : session.video.currentTime},keepalive);
}
window.addEventListener('pagehide',() => { if (videoSession) saveVideoPosition(videoSession,true); });

function videoTracks(session, saved, controls) {
  const {video,hls} = session;
  for (const kind of ['audio','subtitle']) {
    const candidates = hls ? (kind === 'audio' ? hls.audioTracks : hls.subtitleTracks)
      : Array.from(kind === 'audio' ? video.audioTracks || [] : video.textTracks || []);
    if (!candidates.length || (kind === 'audio' && candidates.length === 1)) continue;
    const select = node('select');
    const descriptors = candidates.map((track,i) => {
      const title = track.name || track.label || track.lang || track.language || `Track ${i + 1}`;
      const language = track.lang || track.language || '';
      return {track,title,language,value:['web',language,title].join('\n')};
    });
    if (kind === 'subtitle') {
      const option = node('option','Off'); option.value = 'off'; select.append(option);
    }
    descriptors.forEach(({title,value}) => { const option = node('option',title); option.value = value; select.append(option); });
    function selectTrack(value) {
      const index = descriptors.findIndex(track => track.value === value);
      if (hls) {
        if (kind === 'audio' && index >= 0) hls.audioTrack = index;
        if (kind === 'subtitle') { hls.subtitleTrack = index; hls.subtitleDisplay = index >= 0; }
      } else descriptors.forEach(({track},i) => {
        if (kind === 'audio') track.enabled = i === index;
        else track.mode = i === index ? 'showing' : 'disabled';
      });
    }
    const [format,language,title] = (saved[kind] || '').split('\n');
    const selected = descriptors.find(track => track.title.toLowerCase() === (title || '').toLowerCase()
      && (!language || !track.language || track.language.toLowerCase().startsWith(language.toLowerCase().slice(0,2))));
    if (format === 'off' || selected) {
      select.value = selected?.value ?? 'off';
      selectTrack(select.value);
    } else if (kind === 'audio') {
      select.value = descriptors[hls?.audioTrack ?? candidates.findIndex(track => track.enabled)]?.value ?? descriptors[0].value;
    }
    select.addEventListener('change',() => { selectTrack(select.value); videoUpdate(session,{[kind]:select.value}); });
    controls.append(field(kind === 'audio' ? 'Audio' : 'Subtitles',select));
  }
}

async function watch(ep, show, trigger, clip) {
  closeVideo();
  audio.pause();
  const video = node('video');
  video.controls = true;
  video.autoplay = true;
  video.playsInline = true;
  video.setAttribute('aria-labelledby','videoTitle');
  const session = {video,trigger,scroll:window.scrollY,hls:null,identifier:ep.video.id,ready:false,last:0};
  videoSession = session;

  const back = button('Back',() => closeVideo(true),'video-back');
  back.prepend(icon('back'));
  const bar = node('header','','video-bar');
  const controls = node('div','','actions video-options');
  bar.append(back,controls);
  if (document.pictureInPictureEnabled || video.webkitSetPresentationMode) {
    controls.append(button('Picture in Picture',async () => {
      try {
        if (document.pictureInPictureElement) await document.exitPictureInPicture();
        else if (video.requestPictureInPicture && document.pictureInPictureEnabled) await video.requestPictureInPicture();
        else video.webkitSetPresentationMode(video.webkitPresentationMode === 'picture-in-picture' ? 'inline' : 'picture-in-picture');
      } catch (error) { note.textContent = error.message; }
    }));
  }
  const stage = node('div','','video-stage');
  const note = status('Opening video…');
  note.classList.add('video-status');
  stage.append(video,note);
  const info = node('div','','video-info');
  const heading = node('h1',ep.title);
  heading.id = 'videoTitle';
  const identity = node('div');
  identity.append(heading,node('small',show.title));
  const source = anchor(`Open on ${ep.video.label}`,ep.video.url,'video-source');
  source.target = '_blank';
  source.rel = 'noopener noreferrer';
  info.append(identity,source);

  videoPage.replaceChildren(bar,stage,info);
  main.hidden = true;
  videoPage.hidden = false;
  document.body.classList.add('is-watching');
  skipLink.hash = videoPage.id;
  window.scrollTo(0,0);
  back.focus({preventScroll:true});
  video.addEventListener('canplay',() => { note.textContent = ''; },{once:true});
  const showFailure = () => { note.textContent = 'The video could not be played here.'; };
  video.addEventListener('error',showFailure);
  for (const event of ['pause','seeked','ended']) video.addEventListener(event,() => {
    if (videoSession === session) saveVideoPosition(session);
  });
  video.addEventListener('timeupdate',() => {
    if (videoSession === session && !video.paused && Date.now() - session.last >= 12000) {
      session.last = Date.now();
      saveVideoPosition(session);
    }
  });
  try {
    const query = new URLSearchParams({show:show.id,episode:ep.id});
    if (clip) query.set('clip',clip);
    const playback = id => id ? api('/api/video-playback/' + encodeURIComponent(id)).catch(()=>({})) : {};
    const [stream,initialState] = await Promise.all([api(`/api/video?${query}`),playback(session.identifier)]);
    if (videoSession !== session) return;
    const resolved = stream.video || ep.video;
    let saved = initialState;
    if (resolved.id !== session.identifier) {
      session.identifier = resolved.id;
      source.href = resolved.url;
      source.textContent = `Open on ${resolved.label}`;
      saved = await playback(resolved.id);
      if (videoSession !== session) return;
    }
    video.addEventListener('loadedmetadata',() => {
      if (saved.position > 0 && (video.duration > saved.position || !Number.isFinite(video.duration))) video.currentTime = saved.position;
      session.ready = true;
      videoTracks(session,saved,controls);
    },{once:true});
    const url = stream.path || stream.url;
    const nativeHLS = video.canPlayType('application/vnd.apple.mpegurl');
    // Chromium advertises native HLS but can stall on YouTube's separate audio and video playlists.
    if (window.Hls?.isSupported() && (!nativeHLS || resolved.label === 'YouTube')) {
      session.hls = new window.Hls({enableWorker:false});
      session.hls.on(window.Hls.Events.ERROR,(_event,data) => { if (data.fatal) showFailure(); });
      session.hls.loadSource(url);
      session.hls.attachMedia(video);
    } else if (nativeHLS) {
      video.src = url;
    } else {
      note.textContent = 'This browser cannot play this video format.';
    }
  } catch(error) {
    if (videoSession === session) note.textContent = error.message;
  }
}
async function detail(show, force = false, fromSubscriptions = false) {
  const id = ++request;
  const returnTo = fromSubscriptions ? subscriptions : library;
  setPage(fromSubscriptions ? 'subscriptions' : 'library',show.title);
  const back = button(fromSubscriptions ? '‹ Subscriptions' : '‹ Library',returnTo,'breadcrumb');
  main.replaceChildren(back,status('Loading episodes…'));
  try {
    const feed = await api(`/api/shows/${show.id}${force ? '?refresh=1':''}`); if (id !== request) return;
    const header = node('section','','detail'); const summary = node('div');
    const actions = node('div','','actions detail-actions');
    show.minimumEpisodeMinutes = feed.minimumEpisodeMinutes;
    actions.append(anchor('RSS Feed',show.rss,'quiet'),button('Refresh', () => detail(show,true,fromSubscriptions)),
      button('Show Settings', () => settings(show,{onSave:() => detail(show,false,fromSubscriptions),onRemove:returnTo})));
    if (feed.banner?.startsWith('https://')) {
      const background = cover(feed.banner); background.classList.add('detail-background');
      header.append(background);
    }
    if (show.publisher) summary.append(node('small',show.publisher));
    summary.append(node('h1',show.title));
    if (feed.hosts?.length) {
      const credits = node('div','','host-credits');
      for (const host of feed.hosts) {
        const credit = node('span','','host-credit');
        if (host.portrait) credit.append(cover(host.portrait));
        credit.append(node('span',host.name));
        credits.append(credit);
      }
      summary.append(credits);
    }
    if (feed.description) summary.append(node('p',feed.description,'muted'));
    header.append(cover(feed.artwork || show.artwork),summary);
    const query = searchField('Filter Episodes');
    const tableHead=node('div','','heading'); tableHead.append(node('h2','Episodes'),query);
    const table = node('ul','','rows');
    function draw() {
      const matches=feed.episodes.filter(ep => ep.title.toLocaleLowerCase().includes(query.value.toLocaleLowerCase().trim()));
      table.replaceChildren(...matches.map(ep=>{
        return episodeRow({episode:ep,show,progress:progress[ep.id]},null,draw,false,false);
      }));
      if (!matches.length) table.append(node('li','No episodes found.','status'));
    }
    query.addEventListener('input',draw);
    summary.append(actions);
    main.replaceChildren(back,header,tableHead,table);
    if (feed.radioFranceArchive) {
      const archive = node('div','','row');
      archive.append(anchor('Radio France Archive',feed.radioFranceArchive,'quiet'));
      table.after(archive);
      archive.classList.add('archive-link');
    }
    draw();
  } catch(error) {if (id===request) main.replaceChildren(back,status(error.message));}
}
function settings(show, {onSave, onRemove} = {}) {
  const page = request;
  const form = node('form','','subscription-settings');
  const minutes = node('input');
  minutes.type = 'number';
  minutes.min = '0';
  minutes.required = true;
  minutes.value = show.minimumEpisodeMinutesOverride ?? show.minimumEpisodeMinutes ?? 0;
  const notify = node('input'); notify.type = 'checkbox'; notify.checked = !!show.notify;
  const note = status('');
  const actions = node('div','','actions');
  const save = node('button','Save','add');
  const confirmation = node('div');
  confirmation.hidden = true;
  const unsubscribe = button('Unsubscribe',() => {
    actions.hidden = true;
    confirmation.hidden = false;
    cancel.focus();
  },'danger');
  const cancel = button('Cancel',() => {
    confirmation.hidden = true;
    actions.hidden = false;
    unsubscribe.focus();
  });
  const remove = button('Confirm Unsubscribe',() =>
    update('DELETE',undefined,() => {
      shows = shows.filter(existing => existing.id !== show.id);
    },onRemove),'danger');
  const confirmActions = node('div','','actions');
  confirmActions.append(cancel,remove);
  confirmation.append(node('p',`Unsubscribe from ${show.title}?`),confirmActions);
  async function update(method, payload, apply, refresh) {
    save.disabled = unsubscribe.disabled = cancel.disabled = remove.disabled = true;
    note.textContent = '';
    try {
      await api(`/api/shows/${show.id}`,payload,method);
      apply();
      if (form.isConnected && dialog.open) dialog.close();
      if (page === request) await refresh?.();
    } catch (error) { note.textContent = error.message; }
    finally { save.disabled = unsubscribe.disabled = cancel.disabled = remove.disabled = false; }
  }
  form.addEventListener('submit',async event => {
    event.preventDefault();
    if (!confirmation.hidden) return;
    const value = minutes.valueAsNumber;
    if (notify.checked && window.Notification?.permission === 'default') {
      await window.Notification.requestPermission();
    }
    await update('PUT',{minimumEpisodeMinutesOverride:value,notify:notify.checked},() => {
      show.minimumEpisodeMinutesOverride = value;
      show.notify = notify.checked;
    },onSave);
  });
  const notifyLabel = node('label','','check-option');
  notifyLabel.append(notify,node('span','Notify me about new episodes'));
  form.append(field('Hide episodes shorter than (minutes)',minutes),notifyLabel);
  form.append(button('Video Matching', () => videoSources(show, () => {
    dialog.setAttribute('aria-labelledby',heading.id);
    openDialog(heading,form);
  })));
  if (show.rss) {
    const url = node('input');
    url.type = 'url';
    url.readOnly = true;
    url.value = show.rss;
    const copy = button('Copy',async () => {
      try {
        await navigator.clipboard.writeText(url.value);
        note.textContent = 'Feed URL copied.';
      } catch {
        url.focus();
        url.select();
        note.textContent = 'Copy the selected feed URL.';
      }
    });
    const feed = node('div','','subscription-feed');
    feed.append(field('Private RSS Feed',url),copy);
    form.append(feed);
  }
  actions.append(save,unsubscribe);
  form.append(actions,confirmation,note);
  const heading = node('h2',show.title);
  heading.id = 'subscription-settings-title';
  dialog.setAttribute('aria-labelledby',heading.id);
  openDialog(heading,form);
}

function videoSelect(options, selected) {
  const select = node('select');
  for (const [value,label] of options) {
    const option = node('option',label);
    option.value = value;
    select.append(option);
  }
  select.value = selected;
  return select;
}

async function videoEditor(title, subtitle, path, back, render) {
  const heading = node('h2',title);
  heading.id = 'video-editor-title';
  dialog.setAttribute('aria-labelledby', heading.id);
  const note = status('Loading…');
  openDialog(heading,note,button('Back',back));
  try {
    const data = await api(path);
    if (!heading.isConnected || !dialog.open) return;
    note.textContent = '';
    const form = node('form','','video-editor');
    const {payload, saved} = render(form,data);
    const save = node('button','Save','add');
    const actions = node('div','','actions');
    actions.append(button('Back',back),save);
    form.append(actions,note);
    form.addEventListener('submit',async event => {
      event.preventDefault();
      save.disabled = true;
      note.textContent = 'Saving…';
      try {
        await api(path,payload(),'PUT');
        if (form.isConnected) saved(note);
      } catch (error) {
        if (form.isConnected) note.textContent = error.message;
      } finally { save.disabled = false; }
    });
    dc.replaceChildren(heading,node('p',subtitle,'video-editor-subtitle'),form);
    form.querySelector('input, select, textarea')?.focus();
  } catch (error) {
    if (heading.isConnected) note.textContent = error.message;
  }
}

function videoSources(show, back) {
  const profileTitles = {
    standard:'Standard', 'c-dans-l-air':"C dans l’air", 'c-ce-soir':'C ce soir',
    'c-politique':'C politique', rivenzi:'Rivenzi', storiavoce:'Storiavoce',
    '28-minutes':'28 Minutes', 'focus-fondamental':'Focus Fondamental',
    'la-grande-librairie':'La Grande Librairie'
  };
  videoEditor('Video Matching',show.title,`/api/video-matching/${show.id}`,back,(form,config) => {
    const channel = node('input');
    channel.type = 'url';
    channel.placeholder = 'https://www.youtube.com/@channel';
    channel.value = config.youtube;
    const catalogues = node('textarea');
    catalogues.rows = 2;
    catalogues.placeholder = 'https://www.france.tv/france-5/programme/';
    catalogues.value = config.franceCatalogues.join('\n');
    const order = videoSelect([['franceTV','France.tv first'],['youtube','YouTube first']],config.providerOrder[0]);
    const fallback = node('input'); fallback.type = 'checkbox'; fallback.checked = config.fallback;
    const backup = node('label','','checkbox');
    backup.append(fallback,node('span','Try the other provider if playback fails'));
    const profile = videoSelect(config.profiles.map(id => [id,profileTitles[id] || id]),config.profile);
    const options = node('details','','video-match-rules');
    options.append(node('summary','Matching Rules'),field('Matching Profile',profile));
    const rules = node('div','','video-rules-grid');
    const limits = [
      ['dateWindowDays','Date Window (days)',45],
      ['maxDurationDifferenceSeconds','Duration Difference (seconds)',3600],
      ['minimumScore','Minimum Match Score (0–140)',140]
    ];
    const inputs = limits.map(([key,label,max]) => {
      const input = node('input');
      input.type = 'number'; input.min = '0'; input.max = String(max);
      input.placeholder = 'Profile default';
      input.value = config.rules[key] ?? '';
      rules.append(field(label,input));
      return [key,input];
    });
    options.append(rules);
    form.append(field('YouTube Channel',channel),field('France.tv Catalogues (one per line)',catalogues),
      field('Provider Order',order),backup,options);
    return {
      payload: () => ({
        youtube:channel.value, franceCatalogues:catalogues.value.split(/\r?\n/).map(url => url.trim()).filter(Boolean),
        providerOrder: order.value === 'franceTV' ? ['franceTV','youtube'] : ['youtube','franceTV'],
        fallback:fallback.checked, profile:profile.value,
        rules:Object.fromEntries(inputs.filter(([,input]) => input.value !== '').map(([key,input]) => [key,input.valueAsNumber]))
      }),
      saved: note => { note.textContent = 'Saved. Ondier will refresh automatic matches.'; }
    };
  });
}

function editEpisodeVideo(episode, show, back, reload) {
  videoEditor('Edit Video Match',episode.title,`/api/video-matching/${show.id}/${episode.id}`,back,(form,config) => {
    const mode = videoSelect([['automatic','Automatic'],['manual','Custom Replay'],['hidden','Hide Video']],config.mode);
    const url = node('input');
    url.type = 'url';
    url.value = config.url || '';
    url.placeholder = 'Paste a YouTube or France.tv replay URL';
    const address = field('Replay URL',url);
    function updateMode() {
      const manual = mode.value === 'manual';
      address.hidden = !manual;
      url.required = manual;
      if (manual && !url.value && episode.video?.url) url.value = episode.video.url;
    }
    mode.addEventListener('change',updateMode);
    updateMode();
    form.append(field('Video Match',mode),address);
    return {
      payload: () => ({mode:mode.value,url:url.value}),
      saved: () => {
        dialog.close();
        if (reload) reload();
        else detail(show);
      }
    };
  });
}
async function followPodcast(address, source, control, note) {
  control.disabled = true;
  note.textContent = 'Adding podcast…';
  try {
    const show = await api('/api/follow',{url:address,source});
    if (!shows.some(existing => existing.id === show.id)) shows.push(show);
    note.textContent = `Added ${show.title}.`;
    return show;
  } catch (error) {
    note.textContent = error.message;
    control.disabled = false;
    return null;
  }
}
function discoveryRow(item, scope, note) {
  const row = node('li','','row');
  const result = node('div','','result');
  const title = item.title || item.collectionName;
  const publisher = item.handle || item.artistName;
  const artwork = cover(item.artwork || item.artworkUrl100);
  artwork.classList.add('mini-cover');
  const label = node('div');
  label.append(node('strong',title));
  if (publisher) label.append(node('small',publisher));
  result.append(artwork,label);
  const address = item.feedUrl || `https://www.youtube.com/channel/${item.id}/videos`;
  const subscribed = shows.some(show => show.url === address);
  const add = button(subscribed ? 'Subscribed' : 'Add',async () => {
    if (await followPodcast(address,scope,add,note)) {
      add.textContent = 'Subscribed';
      add.setAttribute('aria-label',`Subscribed to ${title}`);
    }
  });
  add.disabled = subscribed;
  add.setAttribute('aria-label',subscribed ? `Subscribed to ${title}` : `Add ${title}`);
  row.append(result,add);
  return row;
}
function discover(fromSubscriptions = false) {
  ++request;
  setPage(fromSubscriptions ? 'subscriptions' : 'discover','Add Podcast');
  const form = node('form','','search');
  const url = node('input');
  url.type = 'url';
  url.placeholder = 'RSS or YouTube channel URL';
  url.required = true;
  url.setAttribute('aria-label',url.placeholder);
  const add = node('button','Add by URL','add');
  const note = status('');
  form.append(url,add);
  form.addEventListener('submit',async event => {
    event.preventDefault();
    const address = url.value.trim();
    const host = new URL(address).hostname;
    const source = /^(?:www\.|m\.)?youtube\.com$/i.test(host) ? 'youtube' : 'apple';
    if (await followPodcast(address,source,add,note)) {
      url.value = '';
      add.disabled = false;
      url.focus();
    }
  });
  const back = fromSubscriptions ? button('‹ Subscriptions',subscriptions,'breadcrumb') : button('‹ Library',library,'breadcrumb');
  main.replaceChildren(back,node('h1','Add Podcast'),form,note,
    button('Search Apple Podcasts or YouTube',() => searchPage('', 'apple')));
  url.focus();
}
function resetSession() {
  closeVideo();
  setSignedIn(false);
  if (dialog.open) dialog.close();
  stopPlayer(false);
  searchState.scope = defaultSearchScope;
  searchState.query = '';
  subscriptionFilter = '';
  shows = [];
  progress = {};
  ++request;
}
function enterLibrary() {
  setSignedIn(true);
  const connect = new URLSearchParams(location.search).get('connect');
  if (connect && /^[A-Za-z0-9_-]{32}$/.test(connect)) location.replace('/connect/' + connect);
  else { library(); checkNotifications(); }
}

async function checkNotifications() {
  if (!signedIn || window.Notification?.permission !== 'granted') return;
  const key = 'ondier-notification-cursor';
  const since = Number(localStorage.getItem(key)) || notificationCursor;
  try {
    const next = await api('/api/notifications?since=' + encodeURIComponent(since));
    if (!signedIn) return;
    for (const {show,episode} of next.episodes) {
      const reminder = new window.Notification(show.title,{body:episode.title,tag:episode.id});
      reminder.onclick = () => { window.focus(); detail(show); };
    }
    notificationCursor = next.checkedAt;
    localStorage.setItem(key,String(notificationCursor));
  } catch {}
}
function login(setup, requireSetupKey) {
  setSignedIn(false);
  const form = node('form', '', 'account-form');
  const username = node('input');
  const password = node('input');
  username.required = true; username.maxLength = 80; username.autocomplete = 'username';
  username.autocapitalize = 'none'; username.spellcheck = false;
  password.type = 'password'; password.required = true; password.maxLength = 256;
  password.autocomplete = setup ? 'new-password' : 'current-password';
  if (setup) { password.minLength = 8; password.setAttribute('aria-describedby', 'password-help'); }
  const nameLabel = node('label', 'Username'); nameLabel.append(username);
  const passwordLabel = node('label', 'Password'); passwordLabel.append(password);
  const submit = node('button', setup ? 'Create Account' : 'Sign In', 'add');
  const note = status('');
  form.append(nameLabel, passwordLabel);
  const setupKey = node('input');
  if (setup && requireSetupKey) {
    setupKey.type = 'password'; setupKey.required = true; setupKey.maxLength = 256;
    setupKey.autocomplete = 'off';
    form.append(field('Setup Key', setupKey));
  }
  if (setup) {
    const help = node('small', 'At least 8 characters.'); help.id = 'password-help'; form.append(help);
  }
  form.append(submit, note);
  form.addEventListener('submit', async event => {
    event.preventDefault();
    submit.disabled = true;
    note.textContent = '';
    try {
      const payload = {username:username.value, password:password.value};
      if (setup && requireSetupKey) payload.setupKey = setupKey.value;
      await api('/api/account/' + (setup ? 'setup' : 'login'), payload);
      password.value = '';
      setupKey.value = '';
      enterLibrary();
    } catch(error) { note.textContent = error.message; }
    finally { submit.disabled = false; }
  });
  main.replaceChildren(node('h1', setup ? 'Create your Ondier account' : 'Sign in to Ondier'), form);
  username.focus();
}
async function start() {
  try {
    const state = await api('/api/account');
    if (state.account) enterLibrary();
    else login(state.setup, state.requireSetupKey);
  } catch(error) {
    main.replaceChildren(status(error.message), button('Retry', start));
  }
}
async function keys() {
  dc.replaceChildren(node('h2', 'API Keys'), status('Loading…'));
  dialog.showModal();
  try {
    const data = await api('/api/account/keys');
    if (!dialog.open || !signedIn) return;
    const form = node('form', '', 'account-form');
    const name = node('input');
    name.required = true; name.maxLength = 80; name.placeholder = 'Roan';
    const nameLabel = node('label', 'Name'); nameLabel.append(name);
    const expiry = node('select');
    for (const [days, label] of [[0, 'Never'], [30, '30 days'], [90, '90 days'], [365, '1 year']]) {
      const option = node('option', label); option.value = days; expiry.append(option);
    }
    const expiryLabel = node('label', 'Expires'); expiryLabel.append(expiry);
    const create = node('button', 'Create API Key', 'add');
    form.append(nameLabel, expiryLabel, create);
    const list = node('ul', '', 'rows');
    const issued = node('div', '', 'stack');
    const note = status('');
    function addKey(record) {
      const row = node('li', '', 'row'), info = node('div');
      const expiration = record.expires ? new Date(record.expires * 1000) : null;
      info.append(node('strong', record.name),
        node('small', expiration ? `${expiration <= new Date() ? 'Expired' : 'Expires'} ${expiration.toLocaleDateString()}` : 'Never expires'));
      const revoke = button('Revoke', async () => {
        if (!confirm(`Revoke the key “${record.name}”?`)) return;
        revoke.disabled = true;
        try {
          await api('/api/account/keys/' + record.id, undefined, 'DELETE');
          if (issued.dataset.id === record.id) issued.replaceChildren();
          row.remove();
          note.textContent = 'API key revoked.';
        } catch(error) { note.textContent = error.message; revoke.disabled = false; }
      }, 'danger');
      row.append(info, revoke);
      return row;
    }
    list.append(...data.keys.map(addKey));
    form.addEventListener('submit', async event => {
      event.preventDefault(); create.disabled = true; note.textContent = '';
      try {
        const record = await api('/api/account/keys', {name:name.value, days:Number(expiry.value)});
        if (!dialog.open || !signedIn) return;
        const value = node('input'); value.value = record.key; value.readOnly = true;
        value.setAttribute('aria-label', 'New API key');
        issued.dataset.id = record.id;
        issued.replaceChildren(node('p', 'Copy this key now. It is shown only once.'), value,
          button('Copy API Key', async () => {
            try { await navigator.clipboard.writeText(value.value); note.textContent = 'API key copied.'; }
            catch { value.select(); note.textContent = 'Use your keyboard to copy the selected key.'; }
          }));
        list.prepend(addKey(record)); name.value = ''; value.focus(); value.select();
      } catch(error) { note.textContent = error.message; }
      finally { create.disabled = false; }
    });
    dc.replaceChildren(node('h2', 'API Keys'), form, issued, list, note);
  } catch(error) { if (dialog.open) dc.replaceChildren(status(error.message)); }
}
async function signOut() {
  try {
    audio.pause();
    await savePlayback();
    await api('/api/account/logout', {});
    resetSession();
    login(false);
  } catch(error) { showError(error); }
}
async function syncSetup() {
  dc.replaceChildren(node('h2','Sync setup'),status('Loading…'));dialog.showModal();
  try {
    const data=await api('/api/sync-settings');dc.replaceChildren(node('h2','Sync setup'),node('p','In a compatible podcast app, choose gPodder Sync or Nextcloud gPodder Sync.'));
    for(const [label,key] of [['Server','server'],['Username','username'],['App password','password']]){
      const input=node('input');input.value=data[key];input.readOnly=true;input.type=key==='password'?'password':'text';
      const field=node('label',label);field.append(input);dc.append(field);
      if(key==='password')dc.append(button('Show password',event=>{input.type=input.type==='password'?'text':'password';event.currentTarget.textContent=input.type==='password'?'Show password':'Hide password';}));
    }
  }catch(error){dc.replaceChildren(status(error.message));}
}
function openDialog(...contents) {
  dc.replaceChildren(...contents);
  if (!dialog.open) dialog.showModal();
}

function episodeActions(episode, show) {
  const actions = node('div','','actions');
  actions.append(iconButton('play',`Play audio: ${episode.title}`, () => play(episode,show),'Play Audio','episode-action'));
  if (episode.video) actions.append(iconButton('video',`Watch video: ${episode.title}`,
    event => watch(episode,show,event.currentTarget),`Watch Video (${episode.video.label})`,'episode-action'));
  return actions;
}

async function episodeMenu(episode,show,trigger,reload,queueIndex) {
  const note = status('Loading…');
  const actions = node('div','','player-option-list');
  const heading = node('h2',episode.title);
  heading.id = 'episode-options-title';
  dialog.setAttribute('aria-labelledby',heading.id);
  openDialog(heading,actions,note);
  actions.append(button('Edit Video Match',() => editEpisodeVideo(episode,show,
    () => episodeMenu(episode,show,trigger,reload,queueIndex),reload)));
  const action = (title, run) => button(title,async () => {
    try { await run(); }
    catch(error) { note.textContent = error.message; }
  });
  function write(operation) {
    return async () => {
      await api('/api/listening',{operation,id:episode.id},'PUT');
      dialog.close();
      reload?.();
    };
  }
  try {
    const saved = await api('/api/saved?ids=1');
    if (!actions.isConnected || !dialog.open) return;
    note.textContent = '';
    const isSaved = saved.ids.includes(episode.id);
    actions.append(action(isSaved ? 'Remove from Saved Episodes' : 'Save Episode',write(isSaved ? 'unsave' : 'save')));
    if (!queueIndex) actions.append(action('Play Next',write('next')));
    else {
      if (queueIndex.index > 0) actions.append(action('Move Up',write('up')));
      if (queueIndex.index < queueIndex.total - 1) actions.append(action('Move Down',write('down')));
      actions.append(action('Remove from Up Next',write('remove')));
    }
    const played = !progress[episode.id]?.finished;
    actions.append(action(played ? 'Mark as Played' : 'Mark as Unplayed',async () => {
      if (playing?.id === episode.id) stopPlayer();
      const value = {position:played ? episode.duration || 0 : 0,finished:played,updatedAt:Date.now()/1000};
      await api('/api/progress',{id:episode.id,progress:value},'PUT');
      progress[episode.id] = value;
      dialog.close();
      if (reload) reload();
      else trigger.closest('.row')?.querySelector('.episode-state')?.replaceChildren(played ? 'Played' : 'Unplayed');
    }));
    if (episode.clips?.length) {
      actions.append(node('h3','YouTube Clips'));
      for (const clip of episode.clips) {
        const url = new URL(clip.link.url);
        const id = url.searchParams.get('v');
        if (!id) continue;
        actions.append(button(clip.title,() => {
          dialog.close();
          watch({...episode,title:clip.title,video:{id,url:url.href,label:'YouTube'}},show,trigger,id);
        }));
      }
    }
  } catch(error) { note.textContent = error.message; }
}

function episodeRow({episode, show, progress: position, excerpt}, listing, reload, isSearchResult = false, showName = true, queueIndex) {
  if (position) progress[episode.id] = position;
  const tr = node('li', '', 'row archive-row');
  const details = node('div');
  const title = node('div','','episode-title');
  title.append(node('strong', episode.title),iconButton('options',`Episode options: ${episode.title}`,
    event => episodeMenu(episode,show,event.currentTarget,reload,queueIndex),'Episode Options','episode-options'));
  details.append(title);
  const shownDate = episode.date ? new Date(episode.date * 1000).toLocaleDateString() : '';
  const played = position?.finished ? 'Played' : position?.position > 0 ? 'In progress' : '';
  const meta = node('small',[showName ? show.title : '',shownDate,formatDuration(episode.duration)].filter(Boolean).join(' · '));
  meta.append(node('span',played ? ' · ' + played : '','episode-state'));
  details.append(meta);
  if (excerpt && excerpt.trim() !== episode.title) details.append(node('p', excerpt, 'excerpt'));
  const actions = episodeActions(episode,show);
  if (listing?.kind === 'collection') {
    const remove = button('−', async () => {
      remove.disabled = true;
      try {
        await api(`/api/lists/${listing.id}/members`, {episode:episode.id,operation:'remove'}, 'PUT');
        listing.episodes = listing.episodes.filter(id => id !== episode.id);
        reload();
      } catch (error) { showError(error); remove.disabled = false; }
    });
    remove.setAttribute('aria-label', `Remove from collection: ${episode.title}`);
    actions.append(remove);
  }
  if (isSearchResult) {
    const image = cover(show.artwork);
    image.classList.add('mini-cover');
    const result = node('div','','result search-result-identity');
    result.append(image,details);
    tr.append(result,actions);
  } else {
    tr.append(details,actions);
  }
  return tr;
}

async function listeningPage(section) {
  const page = ++request;
  const title = section === 'queue' ? 'Up Next' : 'Saved Episodes';
  const reload = () => listeningPage(section);
  setPage(section,title);
  main.replaceChildren(node('h1',title),status('Loading…'));
  try {
    const data = await api('/api/' + section);
    if (page !== request) return;
    const rows = node('ul','','rows');
    rows.append(...data.episodes.map((entry,index) => episodeRow(entry,null,reload,false,true,
      section === 'queue' ? {index,total:data.episodes.length} : undefined)));
    main.replaceChildren(node('h1',title),data.episodes.length ? rows : status(section === 'queue' ? 'Nothing queued.' : 'No saved episodes.'));
  } catch (error) { if (page === request) main.replaceChildren(node('h1',title),status(error.message)); }
}

function searchPage(initialQuery = searchState.query, initialScope = searchState.scope) {
  const page = ++request;
  setPage('search','Search');
  searchState.scope = initialScope;
  searchState.query = initialQuery;
  const layout = node('section','','search-page');
  const heading = node('h1','Search');
  const form = node('form','','search-workspace');
  form.setAttribute('role','search');
  const input = searchField(searchScopes[initialScope].placeholder);
  input.id = 'searchQuery';
  input.maxLength = 200;
  input.value = initialQuery;
  const field = node('div','','search-entry');
  const clear = iconButton('close','Clear Search',() => {
    input.value = '';
    input.focus();
    reset();
  });
  clear.hidden = !input.value;
  const submit = node('button','Search','add');
  field.append(icon('search'),input,clear,submit);
  const scopes = node('fieldset','','search-scopes');
  scopes.append(node('legend','Search in','visually-hidden'));
  const rows = node('ul','','rows search-results');
  const showMatches = node('div','','search-matches');
  const note = status('');
  const more = button('Load More',() => fetchResults(false),'search-more');
  more.hidden = true;
  let scope = initialScope, query = initialQuery, offset = 0, sequence = 0;

  for (const [value, definition] of Object.entries(searchScopes)) {
    const choice = node('input');
    choice.type = 'radio';
    choice.name = 'searchScope';
    choice.value = value;
    choice.checked = value === initialScope;
    const label = node('label','','search-scope');
    label.append(choice,node('span',definition.label));
    scopes.append(label);
    choice.addEventListener('change',() => {
      scope = value;
      input.placeholder = definition.placeholder;
      input.setAttribute('aria-label',definition.placeholder);
      reset();
    });
  }

  function reset() {
    query = input.value.trim();
    searchState.scope = scope;
    searchState.query = query;
    clear.hidden = !input.value;
    fetchResults(true);
  }
  async function fetchResults(replace) {
    const attempt = ++sequence;
    if (replace) {
      offset = 0;
      rows.replaceChildren();
      showMatches.replaceChildren();
      more.hidden = true;
    }
    if (!query) { note.textContent = ''; return; }
    note.textContent = 'Searching…';
    more.disabled = true;
    try {
      if (scope === 'episodes') {
        const params = new URLSearchParams({view:'search',q:query,offset,limit:30});
        const [data, local] = await Promise.all([
          api(`/api/episodes?${params}`),
          replace ? api('/api/shows-search?q=' + encodeURIComponent(query)).catch(()=>({shows:[]})) : {shows:[]}
        ]);
        if (page !== request || attempt !== sequence) return;
        if (local.shows.length) {
          const matches = node('ul','','rows');
          for (const show of local.shows) {
            const entry = node('li','','row');
            const link = button(show.title,() => detail(show),'quiet');
            const names = (show.hosts || []).map(person => person.name).join(', ');
            entry.append(link,node('small',names || show.publisher));
            matches.append(entry);
          }
          showMatches.append(node('h2','Shows'),matches);
        }
        rows.append(...data.episodes.map(item => episodeRow(item,null,undefined,true)));
        offset = data.offset;
        more.hidden = !data.hasMore;
      } else {
        const params = new URLSearchParams({source:scope,q:query});
        const data = await api(`/api/search?${params}`);
        if (page !== request || attempt !== sequence) return;
        rows.append(...(data.items || data.results || []).map(item => discoveryRow(item,scope,note)));
      }
      note.textContent = rows.children.length || showMatches.children.length ? '' : `No results for “${query}”.`;
    } catch (error) {
      if (page === request && attempt === sequence) note.textContent = error.message;
    } finally {
      if (page === request && attempt === sequence) more.disabled = false;
    }
  }
  input.addEventListener('input',() => {
    clear.hidden = !input.value;
    if (!input.value) reset();
  });
  form.addEventListener('submit',event => {event.preventDefault();reset();});
  form.append(field,scopes);
  layout.append(heading,form,note,showMatches,rows,more);
  main.replaceChildren(layout);
  input.focus();
  if (initialQuery) fetchResults(true);
}

function episodesPage(view, listing) {
  const page = ++request;
  const title = listing?.name ?? {latest:'Latest Episodes',history:'Listening History'}[view];
  setPage(listing ? 'lists' : view,title);
  const heading = node('div', '', 'heading');
  heading.append(node('h1',title));
  if (listing) {
    const actions = node('div', '', 'actions');
    actions.append(anchor('Private RSS Feed',listing.rss,'quiet'),
      button('Edit', async () => {
        try { listEditor(await api('/api/lists'), listing); } catch(error) { showError(error); }
      }));
    if (listing.kind === 'collection') actions.append(button('Add Episodes',
      () => addCollectionEpisodes(listing, () => { if (page === request) load(true); }), 'add'));
    heading.append(actions);
  }
  const form = node('form','','search archive-search');
  const input = searchField('Filter Episodes');
  input.maxLength = 200;
  form.append(input,node('button','Search'));
  const rows = node('ul','','rows');
  const note = status('');
  const more = button('Load More', () => load(false));
  more.hidden = true;
  let term = '', offset = 0, sequence = 0;
  async function load(reset) {
    const attempt = ++sequence;
    if (reset) { offset = 0; rows.replaceChildren(); }
    note.textContent = 'Loading episodes…';
    more.disabled = true;
    try {
      const query = new URLSearchParams({view, q:term, offset, limit:30});
      if (listing) query.set('list',listing.id);
      const data = await api(`/api/episodes?${query}`);
      if (page !== request || attempt !== sequence) return;
      rows.append(...data.episodes.map(item => episodeRow(item,listing, () => load(true))));
      offset = data.offset;
      more.hidden = !data.hasMore; more.disabled = false;
      note.textContent = rows.children.length ? '' : 'No episodes found.';
    } catch (error) {
      if (page === request && attempt === sequence) {note.textContent = error.message;more.disabled = false;}
    }
  }
  form.addEventListener('submit', event => { event.preventDefault(); term = input.value.trim(); load(true); });
  if (listing) main.replaceChildren(button('‹ Playlists',playlists,'breadcrumb'),heading,form,rows,note,more);
  else main.replaceChildren(heading,form,rows,note,more);
  load(true);
}

async function playlists() {
  const page = ++request;
  setPage('lists','Playlists');
  main.replaceChildren(status('Loading playlists…'));
  try {
    const data = await api('/api/lists');
    if (page !== request) return;
    const header = node('div','','heading');
    header.append(node('h1','Playlists'),button('New Playlist', () => listEditor(data), 'add'));
    const list = node('ul','','rows');
    for (const item of data.lists) {
      const tr = node('li','','row');
      const label = node('div');
      label.append(node('strong',item.name), node('small',item.kind === 'smart' ? 'Smart playlist' : 'Collection'));
      tr.append(label,button('Open', () => episodesPage('list',item)));
      list.append(tr);
    }
    main.replaceChildren(header, data.lists.length ? list : status('No playlists yet.'));
  } catch(error) { if (page === request) main.replaceChildren(status(error.message),button('Retry',playlists)); }
}

function field(name, input) {
  const label = node('label',name); label.append(input); return label;
}

function listEditor(data, listing) {
  const form = node('form','','account-form list-form');
  const name = node('input'); name.required = true; name.maxLength = 100; name.value = listing?.name ?? '';
  const kind = node('select');
  const selector = (select, values, value) => {
    for (const [id,title] of values) { const option = node('option',title); option.value = id; select.append(option); }
    select.value = value;
  };
  selector(kind,[['smart','Smart Playlist'],['collection','Collection']],listing?.kind ?? 'smart');
  kind.disabled = Boolean(listing);
  const smart = node('div','','stack');
  const query = node('input'); query.maxLength = 200; query.value = listing?.query ?? '';
  const minimum = node('input'); minimum.type='number'; minimum.min=0; minimum.max=1440; minimum.step=1;
  minimum.value = listing?.minimum ?? 0;
  const category = node('select');
  selector(category,[['','All Categories'],...data.categories.map(value => [value,value])],listing?.category ?? '');
  const sort = node('select');
  selector(sort,[['newest','Newest First'],['oldest','Oldest First'],['longest','Longest First']],listing?.sort ?? 'newest');
  const unplayed = node('input'); unplayed.type = 'checkbox'; unplayed.checked = Boolean(listing?.unplayed);
  const showsBox = node('details', '', 'choose-shows');
  showsBox.append(node('summary','Limit to specific podcasts'));
  const checkboxes = node('div','','pick-items');
  const shows = ordered(data.shows).map(show => {
    const box = node('input'); box.type='checkbox'; box.value=show.id; box.checked = listing?.shows?.includes(show.id) ?? false;
    const label = node('label','','checkbox'); label.append(box,node('span',show.title)); checkboxes.append(label);
    return box;
  });
  showsBox.append(checkboxes);
  const unplayedLabel = node('label','','checkbox');unplayedLabel.append(unplayed,node('span','Unplayed only'));
  smart.append(field('Search Terms',query),field('Minimum Duration (minutes)',minimum),
    field('Category',category),field('Sort',sort),unplayedLabel,showsBox);
  const actions = node('div','','actions');
  const save = node('button','Save','add');
  actions.append(save);
  const note = status('');
  if (listing) actions.append(button('Delete Playlist',async () => {
    if (!confirm(`Delete “${listing.name}”?`)) return;
    try { await api(`/api/lists/${listing.id}`,undefined,'DELETE'); dialog.close(); await playlists(); }
    catch(error) { note.textContent=error.message; }
  },'danger'));
  form.append(field('Name',name),field('Type',kind),smart,actions,note);
  const update = () => { smart.hidden = kind.value !== 'smart'; };
  kind.addEventListener('change',update); update();
  form.addEventListener('submit',async event => {
    event.preventDefault(); save.disabled=true;
    try {
      const payload = {name:name.value,kind:kind.value};
      if (kind.value === 'smart') Object.assign(payload,{query:query.value,minimum:Number(minimum.value),
        sort:sort.value,unplayed:unplayed.checked,category:category.value,shows:shows.filter(box=>box.checked).map(box=>box.value)});
      await api(listing ? `/api/lists/${listing.id}` : '/api/lists', payload, listing ? 'PUT' : 'POST');
      dialog.close();
      await playlists();
    } catch(error) { note.textContent=error.message; }
    finally { save.disabled=false; }
  });
  openDialog(node('h2',listing ? 'Edit Playlist' : 'New Playlist'),form);
  name.focus();
}

function addCollectionEpisodes(listing, reload) {
  const form = node('form','','search');
  const input = searchField('Search Episodes');
  input.required = true;
  input.maxLength = 200;
  const search = node('button','Search');
  form.append(input,search);
  const rows = node('ul','','rows');
  const note = status('');
  let saving = 0;
  let revision = 0;
  function result({episode,show}) {
    const row = node('li','','row');
    const label = node('div');
    label.append(node('strong',episode.title),node('small',show.title));
    const added = listing.episodes.includes(episode.id);
    const action = button(added ? 'Added' : 'Add',async () => {
      saving++;
      search.disabled = action.disabled = true;
      try {
        await api(`/api/lists/${listing.id}/members`,{episode:episode.id,operation:'add'},'PUT');
        listing.episodes.push(episode.id);
        action.textContent = 'Added';
        action.setAttribute('aria-label',`Added ${episode.title}`);
        reload();
      } catch(error) { note.textContent = error.message; action.disabled = false; }
      finally { search.disabled = --saving > 0; }
    });
    action.setAttribute('aria-label',`${added ? 'Added' : 'Add'} ${episode.title}`);
    action.disabled = added;
    row.append(label,action);
    return row;
  }
  form.addEventListener('submit',async event => {
    event.preventDefault();
    const q = input.value.trim();
    if (!q || saving) return;
    const attempt = ++revision;
    rows.replaceChildren();
    note.textContent = 'Searching…';
    try {
      const query = new URLSearchParams({view:'search',q,limit:30});
      const data = await api(`/api/episodes?${query}`);
      if (!form.isConnected || attempt !== revision) return;
      rows.replaceChildren(...data.episodes.map(result));
      note.textContent = data.hasMore ? 'Refine the search to find more episodes.' : rows.children.length ? '' : 'No episodes found.';
    } catch(error) {
      if (form.isConnected && attempt === revision) note.textContent = error.message;
    }
  });
  openDialog(node('h2','Add Episodes'),form,note,rows);
  input.focus();
}

function libraryTools() {
  function navigate(action) { dialog.close(); queueMicrotask(action); }
  function group(title, items) {
    const area = node('section','','settings-group');
    const rows = node('div','','settings-links');
    rows.append(...items);
    area.append(node('h3',title),rows);
    return area;
  }
  const version = node('p','','settings-version');
  openDialog(node('h2','Settings'),
    group('Library',[button('Import OPML', () => navigate(opmlImport)),
      anchor('Export OPML','/library.opml','quiet'),anchor('Download Backup','/api/backup','quiet')]),
    group('Connections',[button('Sync Setup', () => navigate(syncSetup)),
      button('API Keys', () => navigate(keys))]),
    group('Account',[button('Sign Out', () => {dialog.close();signOut();},'danger')]),
    version);
  api('/health').then(data => {
    if (typeof data.version === 'string') version.textContent = `Ondier · Version ${data.version}`;
  }).catch(() => {});
}

function opmlImport(onImport) {
  const file = node('input'); file.type='file'; file.accept='.opml,.xml,text/xml,application/xml,text/x-opml';
  const note = status('');
  const options = node('div','','pick-items opml-items');
  const form = node('form','','account-form');
  const submit = node('button','Import Selected','add');submit.disabled=true;
  let choices = [], revision = 0;
  file.addEventListener('change', async () => {
    const load = ++revision;
    choices=[]; options.replaceChildren();submit.disabled=true;
    const chosen=file.files[0];
    if (!chosen) return;
    if (chosen.size > 1_000_000) {note.textContent='Choose an OPML file under 1 MB.';return;}
    note.textContent='Reading OPML…';
    try {
      const data=await api('/api/opml/preview',{xml:await chosen.text()});
      if (load!==revision || !form.isConnected) return;
      choices=data.feeds.map(feed => {
        const box=node('input');box.type='checkbox';box.value=feed.url;box.disabled=feed.duplicate;box.checked=!feed.duplicate;
        const label=node('label','','checkbox');label.append(box,node('span',feed.title+(feed.duplicate?' · Already subscribed':'')));
        options.append(label);
        box.addEventListener('change',()=>{submit.disabled=!choices.some(input=>input.checked);});
        return box;
      });
      submit.disabled=!choices.some(input=>input.checked);
      note.textContent=data.feeds.length ? `${data.feeds.length.toLocaleString()} feeds found.` : 'No podcast feeds found.';
    } catch(error) { if (form.isConnected && load===revision) note.textContent=error.message; }
  });
  form.addEventListener('submit', async event => {
    event.preventDefault(); submit.disabled=true;file.disabled=true;
    const urls=choices.filter(box=>box.checked).map(box=>box.value);
    if(!urls.length){file.disabled=false;return;}
    try {
      const job=await api('/api/opml/import',{urls});
      async function poll() {
        if(!form.isConnected) return;
        try {
          const result=await api(`/api/opml/jobs/${job.id}`);
          if(!form.isConnected) return;
          if(result.done < result.total) {
            note.textContent=`Importing ${result.done} of ${result.total}…`;
            setTimeout(poll,900);
            return;
          }
          options.replaceChildren();
          note.textContent=`${result.added} added · ${result.duplicates} already subscribed · ${result.failed.length} failed`;
          for(const failure of result.failed) options.append(node('p',`${failure.url} · ${failure.reason}`,'excerpt'));
          file.disabled=false;file.value='';
          if (result.added) onImport?.();
        } catch(error) { note.textContent=error.message;file.disabled=false; }
      }
      poll();
    } catch(error) { note.textContent=error.message;submit.disabled=false;file.disabled=false; }
  });
  form.append(field('OPML File',file),options,submit,note);
  openDialog(node('h2','Import OPML'),form);
}

document.querySelector('#home').addEventListener('click', () => { if (signedIn) library(); });
document.querySelector('#search').addEventListener('click',() => searchPage());
document.querySelector('#library').addEventListener('click',library);
document.querySelector('#subscriptions').addEventListener('click',subscriptions);
document.querySelector('#discover').addEventListener('click',() =>
  discover(document.querySelector('#subscriptions').hasAttribute('aria-current')));
document.querySelector('#archiveSearch').addEventListener('submit', event => {
  event.preventDefault();
  searchPage(archiveInput.value.trim(),'episodes');
});
const macKeyboard = /Mac|iPhone|iPad/.test(navigator.platform);
if (!macKeyboard) document.querySelector('.toolbar-search kbd').textContent = 'Ctrl K';
document.addEventListener('keydown', event => {
  if (videoSession && event.key === 'Escape' && !document.fullscreenElement && !videoSession.video.webkitDisplayingFullscreen) {
    event.preventDefault();
    closeVideo(true);
    return;
  }
  if (signedIn && !dialog.open && (event.metaKey || event.ctrlKey) && event.key.toLowerCase() === 'k') {
    event.preventDefault();
    closeVideo();
    if (!document.querySelector('#searchQuery')) searchPage();
    const input = document.querySelector('#searchQuery');
    input.focus();
    input.select();
  }
});
document.querySelector('#latest').addEventListener('click',() => episodesPage('latest'));
document.querySelector('#history').addEventListener('click',() => episodesPage('history'));
document.querySelector('#saved').addEventListener('click',() => listeningPage('saved'));
document.querySelector('#queue').addEventListener('click',() => listeningPage('queue'));
document.querySelector('#lists').addEventListener('click',playlists);
document.querySelector('#tools').addEventListener('click',libraryTools);
document.querySelector('#audioOptions').addEventListener('click',playbackOptions);
window.setInterval(checkNotifications,60000);
dialog.addEventListener('close',()=>{
  if (!dialog.open) {
    dialog.removeAttribute('aria-labelledby');
    dc.replaceChildren();
  }
});
start();
