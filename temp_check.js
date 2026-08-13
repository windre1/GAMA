
    document.addEventListener('DOMContentLoaded', () => {
        const profilesGrid = document.getElementById('profilesGrid');
        const groupFilterBar = document.getElementById('groupFilterBar');
        const tagFilterBar = document.getElementById('tagFilterBar');
        const searchInput = document.getElementById('searchInput');
        const loading = document.getElementById('loading');
        const errorMessage = document.getElementById('error-message');
        const toast = document.getElementById('toast');
        const addModal = document.getElementById('addModal');

        let allProfiles = [];
        let tagCounts = {};
        let labelCounts = {};
        let groups = [];
        let sites = {};
        let activeTag = null;
        let activeGroup = null; // null = semua, '' = tanpa grup
        let ytAvailable = false;

        // Lisensi per-fitur - "profiles"/"tutorial"/"lisensi" selalu bebas kunci.
        const GATED_FEATURES = ['riset', 'downloader', 'repurpose', 'clipper', 'generate',
            'uploader', 'prospek', 'fb-group', 'shovebot', 'chatbot', 'metadata'];
        let licenseFeatures = {};
        function isFeatureUnlocked(view) {
            if (!GATED_FEATURES.includes(view)) return true;
            const f = licenseFeatures[view];
            return !!(f && f.unlocked);
        }
        let platformAvail = {};
        let uploadPlatform = 'youtube';
        const CARD_UPLOAD_PLATFORMS = ['youtube', 'tiktok', 'facebook', 'instagram', 'pinterest', 'threads', 'x'];

        function isTyping() {
            const el = document.activeElement;
            return el && (el.tagName === 'INPUT' || el.tagName === 'SELECT' || el.tagName === 'TEXTAREA');
        }

        document.addEventListener('keydown', (e) => {
            if (e.key === '/' && !isTyping()) {
                e.preventDefault();
                searchInput.focus();
            } else if (e.key === 'Escape') {
                const settingsModal = document.getElementById('omnivoraSettingsModal');
                if (settingsModal.classList.contains('open')) {
                    settingsModal.classList.remove('open');
                } else if (addModal.classList.contains('open')) {
                    addModal.classList.remove('open');
                } else if (!isTyping()) {
                    searchInput.value = '';
                    applyFilters();
                }
            }
        });

        const hideLoadingTimer = setTimeout(() => {
            if (loading && loading.style.display !== 'none') {
                loading.style.display = 'none';
            }
        }, 3500);

        Promise.all([
            fetch('/api/sites').then(r => r.json()).catch(() => ({ sites: {} })),
            fetch('/api/profiles').then(r => r.json()).catch(() => ({ profiles: [], groups: [] })),
            fetch('/api/license/status').then(r => r.json()).catch(() => ({ features: {} }))
        ]).then(([siteData, profileData, licenseData]) => {
            clearTimeout(hideLoadingTimer);
            loading.style.display = 'none';
            if (profileData && profileData.error) { showError(profileData.error); return; }
            sites = (siteData && siteData.sites) || {};
            allProfiles = (profileData && profileData.profiles) || [];
            tagCounts = (profileData && profileData.tags) || {};
            groups = (profileData && profileData.groups) || [];
            licenseFeatures = (licenseData && licenseData.features) || {};
            try {
                renderAll();
            } catch (renderErr) {
                console.error('Error rendering profiles UI:', renderErr);
            }
        }).catch(err => {
            clearTimeout(hideLoadingTimer);
            loading.style.display = 'none';
            showError('Gagal memuat data. Apakah server berjalan?');
            console.error(err);
        });

        function showError(msg) {
            errorMessage.textContent = msg;
            errorMessage.style.display = 'block';
        }

        function api(url, method, body) {
            return fetch(url, {
                method: method,
                headers: { 'Content-Type': 'application/json' },
                body: body ? JSON.stringify(body) : undefined
            }).then(r => r.json());
        }

        function getProfileByName(name) {
            return allProfiles.find(p => p.name === name) || null;
        }

        function baseProfileName(name) {
            return (name || '').replace('_gmail.com', '');
        }

        function getPostingLabels(profile) {
            if (!profile) return [];
            if (Array.isArray(profile.posting_labels)) return profile.posting_labels.filter(Boolean);
            const legacy = ((profile.posting_label || profile.facebook_label) || '').trim();
            return legacy ? [legacy] : [];
        }

        function getPostingLabel(profile) {
            return getPostingLabels(profile).join(', ');
        }

        function getUploadAccountLabel(profile, platform = uploadPlatform) {
            const base = baseProfileName(profile && profile.name);
            const postingLabel = getPostingLabel(profile);
            return postingLabel ? `${base} | ${postingLabel}` : base;
        }

        // ---------- Filter profil berdasar grup platform ----------
        // Nama grup dicocokkan dengan platform terpilih (mis. grup "SHOPEE AFF"
        // cocok untuk platform shopee). Alias pendek (yt, fb, ig, tt, x, pin)
        // hanya cocok sebagai kata utuh supaya tidak salah tangkap.
        const PLATFORM_GROUP_ALIASES = {
            shopee: ['shopee', 'shope', 'sopi'],
            tiktok: ['tiktok', 'tikto', 'tt'],
            youtube: ['youtube', 'yutub', 'yt'],
            facebook: ['facebook', 'fb'],
            instagram: ['instagram', 'insta', 'ig'],
            pinterest: ['pinterest', 'pin'],
            threads: ['threads', 'thread'],
            x: ['twitter', 'x'],
        };

        function groupMatchesPlatform(groupName, platform) {
            const aliases = PLATFORM_GROUP_ALIASES[platform];
            if (!aliases) return false;
            const g = String(groupName || '').toLowerCase();
            const tokens = g.split(/[^a-z0-9]+/).filter(Boolean);
            return aliases.some(a => a.length >= 4 ? g.includes(a) : tokens.includes(a));
        }

        // Profil yang grupnya cocok dengan platform.
        // - Ada anggota  -> tampilkan anggotanya saja.
        // - Grupnya ada tapi kosong -> tampilkan kosong (bukan semua profil),
        //   supaya user sadar harus memasukkan akun ke grup itu.
        // - Grupnya belum dibuat sama sekali -> tampilkan semua profil.
        function profilesForPlatform(platform) {
            const matched = allProfiles.filter(p => (p.groups || []).some(g => groupMatchesPlatform(g, platform)));
            if (matched.length) return { profiles: matched, filtered: true, emptyGroup: false };
            const groupExists = (groups || []).some(g => groupMatchesPlatform(g, platform));
            if (groupExists) return { profiles: [], filtered: true, emptyGroup: true };
            return { profiles: allProfiles, filtered: false, emptyGroup: false };
        }

        function renderAll() {
            recountTags();
            recountLabels();
            renderGroupBar();
            renderTagBar();
            applyFilters();
            document.getElementById('statProfiles').textContent = allProfiles.length;
            document.getElementById('statGroups').textContent = groups.length;
            document.getElementById('statTags').textContent = Object.keys(tagCounts).length;
            const keep = upAccount.value;
            populateUploadAccounts(keep);
            if (risetInitialized && typeof populateRisetProfiles === 'function') populateRisetProfiles();
            if (typeof fillFbGroupProfiles === 'function') fillFbGroupProfiles();
            if (typeof fillRpVoAccounts === 'function') fillRpVoAccounts();
        }

        // Isi dropdown akun Gmail di panel Repurpose > Voice Over. Top-level
        // (bukan di dalam initRepurposeView) supaya bisa dipanggil dari
        // renderAll() setiap kali daftar profil berubah/dimuat.
        function fillRpVoAccounts() {
            const sel = document.getElementById('rpVoAccount');
            if (!sel) return;
            const keep = sel.value;
            sel.innerHTML = '';
            allProfiles.forEach(p => sel.add(new Option(p.name.replace('_gmail.com', ''), p.name)));
            if (keep && allProfiles.some(p => p.name === keep)) sel.value = keep;
        }

        function recountTags() {
            tagCounts = {};
            allProfiles.forEach(p => p.tags.forEach(t => {
                tagCounts[t] = (tagCounts[t] || 0) + 1;
            }));
            if (activeTag && !tagCounts[activeTag]) activeTag = null;
            if (activeGroup && !groups.includes(activeGroup)) activeGroup = null;
            populateSuggestList('tagSuggestList', tagCounts);
        }

        // Label tidak punya bar filter/manajemen tersendiri di atas (beda dari
        // Tag dan Grup) - cukup dikumpulkan dari label yang sudah dipakai di
        // profil manapun, supaya bisa disarankan (datalist) saat menambah
        // label baru lewat kartu profil / tab Uploader.
        function recountLabels() {
            labelCounts = {};
            allProfiles.forEach(p => getPostingLabels(p).forEach(l => {
                labelCounts[l] = (labelCounts[l] || 0) + 1;
            }));
            populateSuggestList('labelSuggestList', labelCounts);
        }

        function populateSuggestList(datalistId, valuesObj) {
            const dl = document.getElementById(datalistId);
            if (!dl) return;
            dl.innerHTML = '';
            Object.keys(valuesObj).sort((a, b) => a.localeCompare(b)).forEach(v => {
                dl.appendChild(new Option(v, v));
            });
        }

        function chip(text, active) {
            const el = document.createElement('button');
            el.type = 'button';
            el.className = 'filter-chip' + (active ? ' active' : '');
            el.textContent = text;
            return el;
        }

        // ---------- Group bar ----------
        function renderGroupBar() {
            groupFilterBar.innerHTML = '';

            const all = chip(`Semua (${allProfiles.length})`, activeGroup === null);
            all.onclick = () => { activeGroup = null; renderGroupBar(); applyFilters(); };
            groupFilterBar.appendChild(all);

            const noGroupCount = allProfiles.filter(p => !(p.groups || []).length).length;
            const none = chip(`Tanpa Grup (${noGroupCount})`, activeGroup === '');
            none.onclick = () => { activeGroup = (activeGroup === '') ? null : ''; renderGroupBar(); applyFilters(); };
            groupFilterBar.appendChild(none);

            groups.forEach(g => {
                const count = allProfiles.filter(p => (p.groups || []).includes(g)).length;
                const c = chip(`${g} (${count})`, activeGroup === g);
                c.classList.add('group-chip');
                c.onclick = () => { activeGroup = (activeGroup === g) ? null : g; renderGroupBar(); applyFilters(); };

                const del = document.createElement('i');
                del.className = 'fa-solid fa-xmark chip-del';
                del.title = `Hapus grup ${g}`;
                del.onclick = (e) => {
                    e.stopPropagation();
                    if (!confirm(`Hapus grup "${g}"? Profil di dalamnya tidak dihapus, hanya jadi tanpa grup.`)) return;
                    api('/api/groups', 'DELETE', { name: g }).then(data => {
                        if (data.success) {
                            groups = data.groups;
                            allProfiles.forEach(p => { p.groups = (p.groups || []).filter(x => x !== g); });
                            if (activeGroup === g) activeGroup = null;
                            renderAll();
                            showToast(`Grup "${g}" dihapus`, 'info');
                        } else showToast(data.error || 'Gagal', 'error');
                    }).catch(() => showToast('Gagal terhubung ke server', 'error'));
                };
                c.appendChild(del);
                groupFilterBar.appendChild(c);
            });

            // + Buat grup
            const addChip = document.createElement('button');
            addChip.type = 'button';
            addChip.className = 'filter-chip add-chip';
            addChip.innerHTML = '<i class="fa-solid fa-plus"></i> Buat Grup';
            addChip.onclick = () => {
                const input = document.createElement('input');
                input.className = 'chip-input';
                input.placeholder = 'nama grup, Enter';
                addChip.replaceWith(input);
                input.focus();
                let finished = false;
                const cancel = () => { if (!finished) { finished = true; input.replaceWith(addChip); } };
                const save = () => {
                    const name = input.value.trim();
                    if (!name) { cancel(); return; }
                    if (finished) return;
                    finished = true;
                    api('/api/groups', 'POST', { name }).then(data => {
                        if (data.success) {
                            groups = data.groups;
                            renderAll();
                            showToast(`Grup "${name}" dibuat`, 'success');
                        } else {
                            showToast(data.error || 'Gagal membuat grup', 'error');
                            renderGroupBar();
                        }
                    }).catch(() => { showToast('Gagal terhubung ke server', 'error'); renderGroupBar(); });
                };
                input.onkeydown = (e) => {
                    if (e.key === 'Enter') save();
                    else if (e.key === 'Escape') cancel();
                };
                input.onblur = () => { if (input.value.trim()) save(); else cancel(); };
            };
            groupFilterBar.appendChild(addChip);
        }

        // ---------- Tag bar ----------
        function renderTagBar() {
            tagFilterBar.innerHTML = '';
            const all = chip('Semua', activeTag === null);
            all.onclick = () => { activeTag = null; renderTagBar(); applyFilters(); };
            tagFilterBar.appendChild(all);

            Object.keys(tagCounts).sort((a, b) => a.localeCompare(b)).forEach(tag => {
                const c = chip(`${tag} (${tagCounts[tag]})`, activeTag === tag);
                c.onclick = () => { activeTag = (activeTag === tag) ? null : tag; renderTagBar(); applyFilters(); };
                tagFilterBar.appendChild(c);
            });
        }

        // ---------- Filtering ----------
        function applyFilters() {
            const term = searchInput.value.toLowerCase();
            const filtered = allProfiles.filter(p => {
                const matchGroup = activeGroup === null
                    || (activeGroup === '' ? !(p.groups || []).length : (p.groups || []).includes(activeGroup));
                const matchTag = !activeTag || p.tags.includes(activeTag);
                const matchTerm = !term
                    || p.name.toLowerCase().includes(term)
                    || (p.groups || []).some(g => g.toLowerCase().includes(term))
                    || (p.note && p.note.toLowerCase().includes(term))
                    || (getPostingLabel(p) && getPostingLabel(p).toLowerCase().includes(term))
                    || p.tags.some(t => t.toLowerCase().includes(term));
                return matchGroup && matchTag && matchTerm;
            });
            renderProfiles(filtered);
        }

        searchInput.addEventListener('input', applyFilters);

        function renderProfiles(profiles) {
            profilesGrid.innerHTML = '';
            if (profiles.length === 0) {
                profilesGrid.innerHTML = '<div class="no-results">Tidak ada profil yang cocok.</div>';
                return;
            }
            // Render batch pertama langsung (visible above fold), sisanya via rAF
            const FIRST_BATCH = 20;
            const frag = document.createDocumentFragment();
            const first = profiles.slice(0, FIRST_BATCH);
            first.forEach(p => frag.appendChild(buildCard(p)));
            profilesGrid.appendChild(frag);
            if (profiles.length > FIRST_BATCH) {
                requestAnimationFrame(() => {
                    const frag2 = document.createDocumentFragment();
                    profiles.slice(FIRST_BATCH).forEach(p => frag2.appendChild(buildCard(p)));
                    profilesGrid.appendChild(frag2);
                });
            }
        }

        // ---------- Card ----------
        function buildCard(p) {
            const displayName = p.name.replace('_gmail.com', '');
            // Nama folder profil pakai "_" pengganti "@" (mis. "affshove_gmail.com")
            // - tampilkan & salin sebagai email asli "affshove@gmail.com".
            const email = p.name.includes('@') ? p.name : p.name.replace(/_(?=[^_]*$)/, '@');
            const card = document.createElement('div');
            card.className = 'profile-card';

            const lastLaunch = p.last_launched
                ? `<span class="last-launch" title="Terakhir dibuka">${p.last_launched.replace('T', ' ')}</span>`
                : '';

            const uaShort = (p.user_agent.match(/Chrome\/[\d.]+/) || ['Chrome'])[0];

            card.innerHTML = `
                <div class="card-header">
                    <div class="avatar">${escapeHtml(displayName.charAt(0).toUpperCase())}</div>
                    <div class="user-info">
                        <h3 title="${escapeHtml(displayName)}">${escapeHtml(displayName)}</h3>
                        <p title="${escapeHtml(email)}">${escapeHtml(email)}</p>
                        ${lastLaunch}
                    </div>
                    <button type="button" class="icon-btn card-copy" title="Salin email"><i class="fa-solid fa-copy"></i></button>
                    <button type="button" class="icon-btn card-del" title="Hapus profil ini"><i class="fa-solid fa-trash-can"></i></button>
                </div>
                <div class="meta-row">
                    <span class="ua-badge" title="${escapeHtml(p.user_agent)}">
                        <i class="fa-solid fa-fingerprint"></i> ${escapeHtml(uaShort)}
                    </span>
                    <button type="button" class="icon-btn regen-ua" title="Acak ulang user agent"><i class="fa-solid fa-dice"></i></button>
                </div>
                <div class="groups-row"></div>
                <div class="tags-row"></div>
                <div class="password-row"></div>
                <div class="posting-label-row"></div>
                <div class="note-row"></div>
                <div class="card-actions"></div>
                <div class="upload-actions"></div>
            `;

            const passwordRow = card.querySelector('.password-row');
            renderPassword(passwordRow, p);

            const postingLabelRow = card.querySelector('.posting-label-row');
            renderPostingLabel(postingLabelRow, p);

            // Note (catatan)
            const noteRow = card.querySelector('.note-row');
            renderNote(noteRow, p);

            // Salin email profil ke clipboard
            card.querySelector('.card-copy').onclick = async () => {
                if (window.pywebview && window.pywebview.api && window.pywebview.api.clipboard_set) {
                    try { await window.pywebview.api.clipboard_set(email); } catch (e) {}
                } else {
                    try { await navigator.clipboard.writeText(email); } catch (e) {}
                }
                showToast(`Email "${email}" disalin ke clipboard`, 'success');
            };

            // Hapus profil (konfirmasi ganda: dialog + ketik nama)
            card.querySelector('.card-del').onclick = () => {
                if (!confirm(`Hapus permanen profil "${p.name}"?\n\nSemua sesi login, cookie, dan data Chrome profil ini akan hilang dan TIDAK bisa dikembalikan.`)) return;
                const typed = prompt(`Untuk konfirmasi, ketik nama profilnya:\n${displayName}`);
                if (typed === null) return;
                if (typed.trim().toLowerCase() !== displayName.toLowerCase()) {
                    showToast('Nama tidak cocok - penghapusan dibatalkan', 'error');
                    return;
                }
                showToast('Menghapus profil...', 'info');
                api(`/api/profiles/${encodeURIComponent(p.name)}`, 'DELETE')
                    .then(data => {
                        if (data.success) {
                            allProfiles = allProfiles.filter(x => x.name !== p.name);
                            renderAll();
                            showToast(`Profil "${displayName}" dihapus`, 'success');
                        } else {
                            showToast(data.error || 'Gagal menghapus profil', 'error');
                        }
                    }).catch(() => showToast('Gagal terhubung ke server', 'error'));
            };

            // Grup (multi - seperti tag)
            renderGroups(card.querySelector('.groups-row'), p);

            // Regenerate UA
            card.querySelector('.regen-ua').onclick = () => {
                api(`/api/profiles/${encodeURIComponent(p.name)}/regen_ua`, 'POST')
                    .then(data => {
                        if (data.success) {
                            p.user_agent = data.user_agent;
                            applyFilters();
                            showToast('User agent baru dibuat', 'success');
                        } else showToast(data.error || 'Gagal', 'error');
                    }).catch(() => showToast('Gagal terhubung ke server', 'error'));
            };

            // Tags
            const tagsRow = card.querySelector('.tags-row');
            p.tags.forEach(tag => {
                const t = document.createElement('span');
                t.className = 'tag';
                t.innerHTML = `${escapeHtml(tag)} <i class="fa-solid fa-xmark" title="Hapus tag"></i>`;
                t.querySelector('i').onclick = () => {
                    api(`/api/profiles/${encodeURIComponent(p.name)}/tags`, 'DELETE', { tag })
                        .then(data => {
                            if (data.success) { p.tags = data.tags; renderAll(); showToast(`Tag "${tag}" dihapus`, 'info'); }
                            else showToast(data.error || 'Gagal', 'error');
                        }).catch(() => showToast('Gagal terhubung ke server', 'error'));
                };
                tagsRow.appendChild(t);
            });

            const addBtn = document.createElement('button');
            addBtn.type = 'button';
            addBtn.className = 'tag add-tag';
            addBtn.innerHTML = '<i class="fa-solid fa-plus"></i> tag';
            addBtn.onclick = () => {
                const input = document.createElement('input');
                input.className = 'tag-input';
                input.placeholder = 'nama tag, Enter';
                input.setAttribute('list', 'tagSuggestList');
                input.setAttribute('autocomplete', 'off');
                addBtn.replaceWith(input);
                input.focus();
                let finished = false;
                const cancel = () => { if (!finished) { finished = true; input.replaceWith(addBtn); } };
                const save = () => {
                    const tag = input.value.trim();
                    if (!tag) { cancel(); return; }
                    if (finished) return;
                    finished = true;
                    api(`/api/profiles/${encodeURIComponent(p.name)}/tags`, 'POST', { tag })
                        .then(data => {
                            if (data.success) {
                                p.tags = data.tags;
                                renderAll();
                                showToast(`Tag "${tag}" ditambahkan`, 'success');
                            } else {
                                showToast(data.error || 'Gagal menambah tag', 'error');
                                applyFilters();
                            }
                        }).catch(() => { showToast('Gagal terhubung ke server', 'error'); applyFilters(); });
                };
                // Simpan saat Enter ATAU saat klik di luar (blur) - Esc untuk batal
                input.onkeydown = (e) => {
                    e.stopPropagation();
                    if (e.key === 'Enter') save();
                    else if (e.key === 'Escape') cancel();
                };
                input.onblur = () => { if (input.value.trim()) save(); else cancel(); };
            };
            tagsRow.appendChild(addBtn);

            // Site shortcut buttons
            const actions = card.querySelector('.card-actions');
            Object.entries(sites).forEach(([key, site]) => {
                const btn = document.createElement('button');
                btn.type = 'button';
                btn.className = 'btn-action';
                btn.title = `Buka ${site.label}`;
                btn.style.setProperty('--site-color', site.color);
                btn.innerHTML = `<i class="${site.icon}"></i>`;
                btn.onclick = () => launchProfile(p, key, site.label);
                actions.appendChild(btn);
            });

            const uploadActions = card.querySelector('.upload-actions');
            const uploadLabel = document.createElement('span');
            uploadLabel.className = 'upload-actions-label';
            uploadLabel.title = 'Tombol upload per platform';
            uploadLabel.innerHTML = '<i class="fa-solid fa-cloud-arrow-up"></i> Upload';
            uploadActions.appendChild(uploadLabel);
            CARD_UPLOAD_PLATFORMS.forEach((key) => {
                const site = sites[key];
                if (!site) return;
                const btn = document.createElement('button');
                btn.type = 'button';
                btn.className = 'upload-seg-btn';
                btn.title = 'Upload ke ' + site.label + ' dengan akun ini';
                btn.style.setProperty('--site-color', site.color);
                btn.innerHTML = '<i class="' + site.icon + '"></i>';
                btn.onclick = () => openUploadFor(p.name, key);
                uploadActions.appendChild(btn);
            });

            return card;
        }

        // ---------- Grup (multi, seperti tag) ----------
        function renderGroups(row, p) {
            row.innerHTML = '';
            (p.groups || []).forEach(g => {
                const c = document.createElement('span');
                c.className = 'group-chip-card';
                c.innerHTML = `<i class="fa-solid fa-layer-group"></i> ${escapeHtml(g)} <i class="fa-solid fa-xmark" title="Lepas dari grup"></i>`;
                c.querySelector('.fa-xmark').onclick = () => {
                    api(`/api/profiles/${encodeURIComponent(p.name)}/group`, 'DELETE', { group: g }).then(data => {
                        if (data.success) { p.groups = data.groups; renderAll(); showToast(`Dilepas dari grup "${g}"`, 'info'); }
                        else showToast(data.error || 'Gagal', 'error');
                    }).catch(() => showToast('Gagal terhubung ke server', 'error'));
                };
                row.appendChild(c);
            });
            const addBtn = document.createElement('button');
            addBtn.type = 'button';
            addBtn.className = 'group-chip-card add-group';
            addBtn.innerHTML = '<i class="fa-solid fa-plus"></i> grup';
            addBtn.onclick = () => {
                if (!groups.length) { showToast('Buat grup dulu di bar Grup di atas', 'info'); return; }
                const avail = groups.filter(g => !(p.groups || []).includes(g));
                if (!avail.length) { showToast('Profil sudah masuk semua grup', 'info'); return; }
                const sel = document.createElement('select');
                sel.className = 'group-add-select';
                sel.add(new Option('pilih grup...', ''));
                avail.forEach(g => sel.add(new Option(g, g)));
                addBtn.replaceWith(sel);
                sel.focus();
                let done = false;
                const restore = () => { if (done) return; done = true; try { sel.replaceWith(addBtn); } catch (e) {} };
                sel.onchange = () => {
                    const g = sel.value;
                    if (!g) { restore(); return; }
                    done = true;
                    api(`/api/profiles/${encodeURIComponent(p.name)}/group`, 'POST', { group: g }).then(data => {
                        if (data.success) { p.groups = data.groups; renderAll(); showToast(`Masuk grup "${g}"`, 'success'); }
                        else { showToast(data.error || 'Gagal', 'error'); renderAll(); }
                    }).catch(() => { showToast('Gagal terhubung ke server', 'error'); renderAll(); });
                };
                sel.onblur = restore;
            };
            row.appendChild(addBtn);
        }

        function maskPassword(value) {
            const n = Math.max(6, Math.min(String(value || '').length, 18));
            return '?'.repeat(n);
        }

        function renderPassword(passwordRow, p) {
            passwordRow.innerHTML = '';

            const shell = document.createElement('div');
            shell.className = 'password-shell';

            const view = document.createElement('button');
            view.type = 'button';
            view.className = 'password-view' + (p.password ? '' : ' password-empty');
            if (p.password) {
                view.title = 'Klik untuk edit password Gmail';
                view.innerHTML = `<i class="fa-solid fa-key"></i><span></span>`;
                view.querySelector('span').textContent = `Password Gmail: ${maskPassword(p.password)}`;
            } else {
                view.innerHTML = `<i class="fa-solid fa-plus"></i><span>Simpan password Gmail</span>`;
            }
            shell.appendChild(view);

            if (p.password) {
                const showBtn = document.createElement('button');
                showBtn.type = 'button';
                showBtn.className = 'icon-btn password-mini';
                showBtn.title = 'Lihat password';
                showBtn.innerHTML = '<i class="fa-solid fa-eye"></i>';
                showBtn.onclick = (e) => {
                    e.stopPropagation();
                    const shown = showBtn.dataset.shown === '1';
                    showBtn.dataset.shown = shown ? '0' : '1';
                    showBtn.title = shown ? 'Lihat password' : 'Sembunyikan password';
                    showBtn.innerHTML = shown ? '<i class="fa-solid fa-eye"></i>' : '<i class="fa-solid fa-eye-slash"></i>';
                    view.querySelector('span').textContent = shown ? `Password Gmail: ${maskPassword(p.password)}` : `Password Gmail: ${p.password}`;
                };
                shell.appendChild(showBtn);

                const delBtn = document.createElement('button');
                delBtn.type = 'button';
                delBtn.className = 'icon-btn password-mini password-del';
                delBtn.title = 'Hapus password';
                delBtn.innerHTML = '<i class="fa-solid fa-trash-can"></i>';
                delBtn.onclick = (e) => {
                    e.stopPropagation();
                    if (!confirm(`Hapus password tersimpan untuk ${p.name.replace('_gmail.com', '')}?`)) return;
                    api(`/api/profiles/${encodeURIComponent(p.name)}/password`, 'DELETE')
                        .then(data => {
                            if (data.success) {
                                p.password = '';
                                p.has_password = false;
                                renderPassword(passwordRow, p);
                                showToast('Password dihapus', 'info');
                            } else showToast(data.error || 'Gagal menghapus password', 'error');
                        }).catch(() => showToast('Gagal terhubung ke server', 'error'));
                };
                shell.appendChild(delBtn);
            }

            view.onclick = () => {
                const input = document.createElement('input');
                input.type = 'password';
                input.className = 'password-input';
                input.placeholder = 'Masukkan password Gmail...';
                input.autocomplete = 'new-password';
                input.value = p.password || '';
                shell.replaceWith(input);
                input.focus();
                input.setSelectionRange(input.value.length, input.value.length);

                let finished = false;
                const done = () => renderPassword(passwordRow, p);
                const cancel = () => {
                    if (finished) return;
                    finished = true;
                    done();
                };
                const save = () => {
                    if (finished) return;
                    finished = true;
                    const password = input.value;
                    if (password === (p.password || '')) { done(); return; }
                    api(`/api/profiles/${encodeURIComponent(p.name)}/password`, 'POST', { password })
                        .then(data => {
                            if (data.success) {
                                p.password = data.password || '';
                                p.has_password = !!data.has_password;
                                showToast(password ? 'Password disimpan' : 'Password dikosongkan', 'success');
                            } else {
                                showToast(data.error || 'Gagal menyimpan password', 'error');
                            }
                            done();
                        }).catch(() => {
                            showToast('Gagal terhubung ke server', 'error');
                            done();
                        });
                };
                input.onkeydown = (e) => {
                    e.stopPropagation();
                    if (e.key === 'Enter') save();
                    else if (e.key === 'Escape') cancel();
                };
                input.onblur = save;
            };

            passwordRow.appendChild(shell);
        }

        // ---------- Posting label (multi) ----------
        function applyPostingLabelsResult(p, data) {
            p.posting_labels = data.posting_labels || [];
            p.posting_label = data.posting_label || '';
            p.facebook_label = data.facebook_label || '';
            populateUploadAccounts(upAccount.value || p.name);
            if (uploadMode === 'multi') renderMultiAccounts();
            syncPostingLabel();
        }

        function renderPostingLabel(postingLabelRow, p) {
            postingLabelRow.innerHTML = '';

            getPostingLabels(p).forEach(label => {
                const chip = document.createElement('span');
                chip.className = 'label-chip';
                chip.innerHTML = `<i class="fa-solid fa-bookmark"></i> ${escapeHtml(label)} <i class="fa-solid fa-xmark" title="Hapus label"></i>`;
                chip.querySelector('.fa-xmark').onclick = () => {
                    api(`/api/profiles/${encodeURIComponent(p.name)}/posting_labels`, 'DELETE', { posting_label: label })
                        .then(data => {
                            if (data.success) {
                                applyPostingLabelsResult(p, data);
                                renderAll();
                                showToast(`Label "${label}" dihapus`, 'info');
                            } else showToast(data.error || 'Gagal menghapus label', 'error');
                        }).catch(() => showToast('Gagal terhubung ke server', 'error'));
                };
                postingLabelRow.appendChild(chip);
            });

            const addBtn = document.createElement('button');
            addBtn.type = 'button';
            addBtn.className = 'label-chip add-label';
            addBtn.innerHTML = '<i class="fa-solid fa-plus"></i> label';
            addBtn.onclick = () => {
                const input = document.createElement('input');
                input.className = 'tag-input';
                input.placeholder = 'FB Utama, Enter';
                input.setAttribute('list', 'labelSuggestList');
                input.setAttribute('autocomplete', 'off');
                addBtn.replaceWith(input);
                input.focus();
                let finished = false;
                const cancel = () => { if (!finished) { finished = true; input.replaceWith(addBtn); } };
                const save = () => {
                    const label = input.value.trim();
                    if (!label) { cancel(); return; }
                    if (finished) return;
                    finished = true;
                    api(`/api/profiles/${encodeURIComponent(p.name)}/posting_labels`, 'POST', { posting_label: label })
                        .then(data => {
                            if (data.success) {
                                applyPostingLabelsResult(p, data);
                                renderAll();
                                showToast(`Label "${label}" ditambahkan`, 'success');
                            } else {
                                showToast(data.error || 'Gagal menambah label', 'error');
                                applyFilters();
                            }
                        }).catch(() => { showToast('Gagal terhubung ke server', 'error'); applyFilters(); });
                };
                input.onkeydown = (e) => {
                    e.stopPropagation();
                    if (e.key === 'Enter') save();
                    else if (e.key === 'Escape') cancel();
                };
                input.onblur = () => { if (input.value.trim()) save(); else cancel(); };
            };
            postingLabelRow.appendChild(addBtn);
        }

        // ---------- Note ----------
        function renderNote(noteRow, p) {
            noteRow.innerHTML = '';

            const view = document.createElement('button');
            view.type = 'button';
            if (p.note) {
                view.className = 'note-view';
                view.title = 'Klik untuk edit catatan';
                view.innerHTML = `<i class="fa-solid fa-note-sticky"></i> <span></span>`;
                view.querySelector('span').textContent = p.note;
            } else {
                view.className = 'note-view note-empty';
                view.innerHTML = `<i class="fa-solid fa-plus"></i> catatan`;
            }

            view.onclick = () => {
                const ta = document.createElement('textarea');
                ta.className = 'note-input';
                ta.placeholder = 'Tulis catatan... (Ctrl+Enter simpan, Esc batal)';
                ta.value = p.note || '';
                ta.rows = 3;
                view.replaceWith(ta);
                ta.focus();
                ta.setSelectionRange(ta.value.length, ta.value.length);

                let finished = false;
                const cancel = () => {
                    if (finished) return;
                    finished = true;
                    renderNote(noteRow, p);
                };
                const save = () => {
                    if (finished) return;
                    finished = true;
                    const note = ta.value.trim();
                    if (note === (p.note || '')) { renderNote(noteRow, p); return; }
                    api(`/api/profiles/${encodeURIComponent(p.name)}/note`, 'POST', { note })
                        .then(data => {
                            if (data.success) {
                                p.note = note;
                                showToast(note ? 'Catatan disimpan' : 'Catatan dihapus', 'success');
                            } else {
                                showToast(data.error || 'Gagal menyimpan catatan', 'error');
                            }
                            renderNote(noteRow, p);
                        }).catch(() => {
                            showToast('Gagal terhubung ke server', 'error');
                            renderNote(noteRow, p);
                        });
                };
                ta.onkeydown = (e) => {
                    e.stopPropagation();
                    if (e.key === 'Enter' && (e.ctrlKey || e.metaKey)) save();
                    else if (e.key === 'Escape') cancel();
                };
                // Klik di luar = simpan (sama seperti perilaku tag)
                ta.onblur = save;
            };

            noteRow.appendChild(view);
        }

        function launchProfile(p, siteKey, label) {
            showToast(`Membuka ${label}...`, 'info');
            api('/api/launch', 'POST', { profile: p.name, site: siteKey })
                .then(data => {
                    if (data.success) {
                        p.last_launched = new Date().toISOString().slice(0, 19);
                        showToast(data.message, 'success');
                        applyFilters();
                    } else showToast(`Error: ${data.error}`, 'error');
                }).catch(() => showToast('Gagal terhubung ke server', 'error'));
        }

        // ---------- Tambah Akun modal ----------
        const newEmail = document.getElementById('newEmail');
        const newPassword = document.getElementById('newPassword');
        const newGroup = document.getElementById('newGroup');
        const newTag = document.getElementById('newTag');
        const autoLogin = document.getElementById('autoLogin');
        const btnConfirmAdd = document.getElementById('btnConfirmAdd');

        document.getElementById('btnAddAccount').onclick = () => {
            newEmail.value = '';
            newPassword.value = '';
            newTag.value = '';
            autoLogin.checked = true;
            newGroup.innerHTML = '';
            newGroup.add(new Option('Tanpa grup', ''));
            groups.forEach(g => newGroup.add(new Option(g, g)));
            addModal.classList.add('open');
            setTimeout(() => newEmail.focus(), 50);
        };
        document.getElementById('btnCancelAdd').onclick = () => addModal.classList.remove('open');
        addModal.addEventListener('click', (e) => {
            if (e.target === addModal) addModal.classList.remove('open');
        });
        [newEmail, newPassword, newTag].forEach(el => el.addEventListener('keydown', (e) => {
            if (e.key === 'Enter') btnConfirmAdd.click();
        }));

        btnConfirmAdd.onclick = () => {
            const email = newEmail.value.trim();
            if (!email) { showToast('Email wajib diisi', 'error'); return; }
            btnConfirmAdd.disabled = true;
            api('/api/profiles/add', 'POST', {
                email,
                password: newPassword.value,
                tag: newTag.value.trim(),
                auto_login: autoLogin.checked
            }).then(data => {
                btnConfirmAdd.disabled = false;
                if (!data.success) { showToast(data.error || 'Gagal membuat profil', 'error'); return; }
                addModal.classList.remove('open');
                const prof = data.profile;
                const finish = () => {
                    allProfiles.push(prof);
                    allProfiles.sort((a, b) => a.name.toLowerCase().localeCompare(b.name.toLowerCase()));
                    renderAll();
                    showToast(data.launched
                        ? 'Profil dibuat - silakan login di jendela Chrome yang terbuka'
                        : 'Profil baru berhasil dibuat', 'success');
                };
                if (newGroup.value) {
                    api(`/api/profiles/${encodeURIComponent(prof.name)}/group`, 'POST', { group: newGroup.value })
                        .then(g => { if (g.success) prof.groups = g.groups; finish(); })
                        .catch(finish);
                } else finish();
            }).catch(() => {
                btnConfirmAdd.disabled = false;
                showToast('Gagal terhubung ke server', 'error');
            });
        };

        // ---------- Tab / view switching ----------
        const tabButtons = document.querySelectorAll('.tab-btn');
        const PLATFORM_META = {
            youtube: { icon: 'fa-brands fa-youtube', label: 'Upload YouTube' },
            tiktok: { icon: 'fa-brands fa-tiktok', label: 'Upload TikTok' },
            facebook: { icon: 'fa-brands fa-facebook-f', label: 'Upload Facebook' },
            instagram: { icon: 'fa-brands fa-instagram', label: 'Upload Instagram' },
            pinterest: { icon: 'fa-brands fa-pinterest', label: 'Upload Pinterest' },
            threads: { icon: 'fa-brands fa-threads', label: 'Upload Threads' },
            x: { icon: 'fa-brands fa-x-twitter', label: 'Upload X' },
        };
        const ADMIN_WA_NUMBER = '088223705907';
        const ADMIN_WA_URL = `https://wa.me/62${ADMIN_WA_NUMBER.replace(/^0/, '')}`;
        function openAdminWA(label) {
            const msg = encodeURIComponent(`Halo Admin, saya ingin membeli lisensi fitur ${label || ''} GAMA. Mohon informasi harga dan cara aktivasinya.`);
            const url = `${ADMIN_WA_URL}?text=${msg}`;
            api('/api/open_url', 'POST', { url }).then(d => {
                if (!d.success) window.open(url, '_blank');
            }).catch(() => window.open(url, '_blank'));
        }
        function renderLockScreen(view) {
            const info = licenseFeatures[view] || {};
            const label = info.label || view;
            document.getElementById('lockedFeatureTitle').innerHTML =
                `<i class="fa-solid fa-lock"></i> ${escapeHtml(label)} Terkunci`;
            document.getElementById('lockedFeatureMsg').textContent =
                `Fitur "${label}" memerlukan lisensi aktif. Hubungi admin untuk mendapatkan lisensi dan mengaktifkan fitur ini.`;
            // simpan label di tombol supaya handler bisa pakai
            document.getElementById('btnGoToLisensi').dataset.featureLabel = label;
        }

        function switchView(view, btn) {
            // Tab selalu terbuka - tidak ada lock screen.
            // Penguncian hanya terjadi di level tombol aksi (lihat setupActionButtonGuards).
            tabButtons.forEach(b => b.classList.remove('active'));
            if (btn) {
                btn.classList.add('active');
            } else {
                const targetBtn = document.querySelector(`.tab-btn[data-view="${view}"]`);
                if (targetBtn) targetBtn.classList.add('active');
            }
            document.getElementById('view-profiles').classList.toggle('view-hidden', view !== 'profiles');
            document.getElementById('view-riset').classList.toggle('view-hidden', view !== 'riset');
            document.getElementById('view-downloader').classList.toggle('view-hidden', view !== 'downloader');
            document.getElementById('view-repurpose').classList.toggle('view-hidden', view !== 'repurpose');
            document.getElementById('view-clipper').classList.toggle('view-hidden', view !== 'clipper');
            document.getElementById('view-generate').classList.toggle('view-hidden', view !== 'generate');
            document.getElementById('view-uploader').classList.toggle('view-hidden', view !== 'uploader');
            document.getElementById('view-prospek').classList.toggle('view-hidden', view !== 'prospek');
            document.getElementById('view-fb-group').classList.toggle('view-hidden', view !== 'fb-group');
            document.getElementById('view-shovebot').classList.toggle('view-hidden', view !== 'shovebot');
            document.getElementById('view-chatbot').classList.toggle('view-hidden', view !== 'chatbot');
            document.getElementById('view-metadata').classList.toggle('view-hidden', view !== 'metadata');
            document.getElementById('view-tutorial').classList.toggle('view-hidden', view !== 'tutorial');
            document.getElementById('view-lisensi').classList.toggle('view-hidden', view !== 'lisensi');
            document.getElementById('view-locked').classList.add('view-hidden');

            if (view === 'lisensi') initLicenseView();
            if (view === 'shovebot') refreshShovebot();
            if (view === 'prospek') initProspekView();
            if (view === 'fb-group') initFbGroupView();
            if (view === 'metadata') initMetadataView();
            if (view === 'generate') {
                initGenerateView();
                if (window.refreshGenerateProfiles) window.refreshGenerateProfiles();
            }
            if (view === 'riset') initRisetView();
            if (view === 'downloader') initDownloaderView();
            if (view === 'repurpose') initRepurposeView();
            if (view === 'clipper') initClipperView();
            if (view === 'chatbot') initChatbotView();
            else stopChatLogPolling();
        }
        function setUploadPlatform(platform) {
            const next = PLATFORM_META[platform] ? platform : 'youtube';
            upPlatform.value = next;
            const meta = PLATFORM_META[next];
            document.getElementById('uploadTitle').innerHTML = '<i class="' + meta.icon + '"></i> ' + meta.label;
            applyPlatform();
        }
        tabButtons.forEach(b => b.onclick = () => {
            if (b.hasAttribute('disabled')) return;
            if (!b.dataset.view) return; // tombol non-view (mis. Support) urus click sendiri
            switchView(b.dataset.view, b);
        });

        // --- Guard tombol aksi: cek lisensi sebelum eksekusi -----------------
        // Tab tetap terbuka dan isinya kelihatan. Baru terkunci saat user
        // klik tombol aksi di dalam tab yang belum punya lisensi aktif.
        const ACTION_BUTTONS = {
            'riset':      ['btnRisetSearch'],
            'downloader': ['btnDownloaderStart', 'btnDownloaderOpenFolder', 'dlStopBtn', 'btnConvertCsv'],
            'repurpose':  ['rpStartBtn', 'rpCaptureBtn', 'rpI2vBtn', 'rpTrBtn', 'rpTrRewriteBtn', 'rpVoGenerateBtn', 'rpMixBtn'],
            'clipper':    ['clAnalyzeBtn', 'clExportBtn', 'clStopBtn', 'clCancelBtn'],
            'generate':   ['btnImgGenStart', 'btnImgGenStop', 'btnImgGenToVideo', 'btnGenStart', 'btnGenStop', 'btnGenToMixer', 'btnPgGenerate', 'btnVpAnalyze'],
            'uploader':   ['btnStartUpload', 'btnStopUpload', 'btnStopMultiUpload', 'btnPreviewCsv'],
            'prospek':    ['btnGmPhotoDownload', 'btnGmScrape', 'btnGmCheckWa', 'btnGmSendReview'],
            'fb-group':   ['btnPostToGroup', 'btnDetectPostGroups', 'btnCheckPostPrivacy', 'btnScrapeGroups', 'btnAutoJoinGroups'],
            'shovebot':   ['sbStart', 'sbStop', 'sbRefresh', 'sbScan', 'sbLaunch'],
            'chatbot':    ['waSendBtn', 'blastStartBtn'],
            'metadata':   ['btnMcStart'],
        };

        function setupActionButtonGuards() {
            Object.entries(ACTION_BUTTONS).forEach(([feature, btnIds]) => {
                btnIds.forEach(id => {
                    const el = document.getElementById(id);
                    if (!el) return;
                    el.addEventListener('click', (e) => {
                        if (isFeatureUnlocked(feature)) return; // lisensi aktif, lanjut normal
                        e.stopImmediatePropagation();
                        e.preventDefault();
                        const info = licenseFeatures[feature] || {};
                        const label = info.label || feature;
                        openAdminWA(label);
                    }, true); // capture phase supaya intercept sebelum handler lain
                });
            });
        }
        setupActionButtonGuards();
        document.getElementById('btnGoToLisensi').addEventListener('click', () => {
            const label = document.getElementById('btnGoToLisensi').dataset.featureLabel || '';
            openAdminWA(label);
        });

        // Tab Support -> buka grup WhatsApp support (bukan view internal).
        document.getElementById('btnSupportTab').addEventListener('click', () => {
            const url = 'https://chat.whatsapp.com/JCJqaKBvMLx3c7b9QP4uW3';
            api('/api/open_url', 'POST', { url }).then(d => {
                if (!d.success) window.open(url, '_blank');
            }).catch(() => window.open(url, '_blank'));
        });

        // Tab Update -> buka folder Google Drive update (bukan view internal).
        document.getElementById('btnUpdateTab').addEventListener('click', () => {
            const url = 'https://drive.google.com/drive/folders/16DTbXcQPz3k-uiFxWKbHr207GgDYsTMI?usp=sharing';
            api('/api/open_url', 'POST', { url }).then(d => {
                if (!d.success) window.open(url, '_blank');
            }).catch(() => window.open(url, '_blank'));
        });

        // Handle uploader subtab clicks
        document.querySelectorAll('#uploaderSubNav .sub-tab-btn').forEach(sub => {
            sub.onclick = () => {
                document.querySelectorAll('#uploaderSubNav .sub-tab-btn').forEach(b => b.classList.remove('active'));
                sub.classList.add('active');
                setUploadPlatform(sub.dataset.platform);
            };
        });

        // ---------- ShoveBot control hub ----------
        let sbPurchaseUrl = 'https://www.shove.my.id';
        function toggleShovebotControls(installed) {
            document.getElementById('sbControls').classList.toggle('view-hidden', !installed);
            document.getElementById('sbLaunch').classList.toggle('view-hidden', !installed);
            document.querySelector('#view-shovebot .sb-note').classList.toggle('view-hidden', !installed);
            document.getElementById('sbNotInstalled').classList.toggle('view-hidden', installed);
        }

        function renderShovebot(s) {
            const badge = document.getElementById('sbServerBadge');
            const unavail = document.getElementById('sbUnavailable');
            if (s.purchase_url) sbPurchaseUrl = s.purchase_url;
            if (!s.installed) {
                unavail.classList.add('view-hidden');
                document.getElementById('sbPath').classList.add('view-hidden');
                document.getElementById('sbBuyReason').textContent = s.reason || 'Fitur ini membutuhkan aplikasi ShoveBot berlisensi.';
                toggleShovebotControls(false);
                return;
            }
            toggleShovebotControls(true);
            const pathEl = document.getElementById('sbPath');
            pathEl.textContent = 'ShoveBot terdeteksi: ' + (s.dir || s.bot_exe || '');
            pathEl.classList.remove('view-hidden');
            unavail.classList.add('view-hidden');
            badge.textContent = s.server_running ? 'RUNNING' : 'STOPPED';
            badge.className = 'sb-badge ' + (s.server_running ? 'sb-on' : 'sb-off');
            document.getElementById('sbUrl').textContent = s.server_url || '-';
            const list = document.getElementById('sbDevices');
            list.innerHTML = '';
            if (!s.devices || !s.devices.length) {
                list.innerHTML = '<li class="sb-muted">Tidak ada HP terhubung. Sambungkan HP + aktifkan USB Debugging.</li>';
            } else {
                s.devices.forEach(d => {
                    const li = document.createElement('li');
                    const ok = d.state === 'device';
                    li.innerHTML = `<i class="fa-solid fa-mobile-screen ${ok ? 'sb-dev-ok' : 'sb-dev-warn'}"></i> ${escapeHtml(d.serial)} <span class="sb-muted">(${escapeHtml(d.state)})</span>`;
                    list.appendChild(li);
                });
            }
        }

        function refreshShovebot() {
            document.getElementById('sbServerBadge').textContent = 'memeriksa...';
            document.getElementById('sbServerBadge').className = 'sb-badge';
            fetch('/api/shovebot/status').then(r => r.json())
                .then(renderShovebot).catch(() => showToast('Gagal cek status ShoveBot', 'error'));
        }

        document.getElementById('sbRefresh').onclick = refreshShovebot;
        document.getElementById('sbScan').onclick = () => {
            fetch('/api/shovebot/scan').then(r => r.json()).then(d => {
                renderShovebot(Object.assign({ installed: true, server_running:
                    document.getElementById('sbServerBadge').textContent === 'RUNNING',
                    server_url: document.getElementById('sbUrl').textContent }, d));
                showToast(`${(d.devices || []).length} HP terdeteksi`, 'info');
            }).catch(() => showToast('Gagal scan HP', 'error'));
        };
        function sbServerAction(action, btn) {
            btn.disabled = true;
            const old = btn.innerHTML;
            btn.innerHTML = '<i class="fa-solid fa-spinner fa-spin"></i> ' + (action === 'start' ? 'Menyalakan...' : 'Mematikan...');
            api('/api/shovebot/server', 'POST', { action }).then(d => {
                btn.disabled = false; btn.innerHTML = old;
                if (d.error) { showToast(d.error, 'error'); return; }
                showToast(action === 'start' ? 'Server Appium dinyalakan' : 'Server Appium dimatikan', 'success');
                refreshShovebot();
            }).catch(() => { btn.disabled = false; btn.innerHTML = old; showToast('Gagal terhubung ke server', 'error'); });
        }
        document.getElementById('sbStart').onclick = (e) => sbServerAction('start', e.currentTarget);
        document.getElementById('sbStop').onclick = (e) => sbServerAction('stop', e.currentTarget);
        document.getElementById('sbLaunch').onclick = () => {
            api('/api/shovebot/launch', 'POST').then(d => {
                showToast(d.success ? 'Membuka ShoveBot...' : (d.error || 'Gagal membuka'), d.success ? 'success' : 'error');
            }).catch(() => showToast('Gagal terhubung ke server', 'error'));
        };
        document.getElementById('sbBuy').onclick = () => {
            api('/api/shovebot/purchase', 'POST', { url: sbPurchaseUrl }).then(d => {
                if (d && d.error) showToast(d.error, 'error');
                else showToast('Membuka halaman ShoveBot...', 'info');
            }).catch(() => showToast('Buka manual: ' + sbPurchaseUrl, 'info'));
        };
        // ---------- Chat (klien WhatsApp + Scrape & Blast) ----------
        let chatbotInitialized = false;
        let activeChatSub = 'wa';
        let waTimer = null;
        let waQrTimer = null;
        let waActiveSession = null;
        let waActiveChat = null;   // {jid, name}
        let waSessionsCache = [];
        let tgSessionsCache = [];
        let selectedScrapeFile = '';   // CSV terpilih untuk blast
        // Telegram chat client
        let tgcTimer = null;
        let tgcChatTimer = null;
        let tgcActiveSession = null;
        let tgcActiveChat = null;   // {id, name}
        let tgcActiveTopicId = null;   // topic aktif kalau chat-nya grup forum (mis. TYM)
        let tgcTopicsCache = [];
        let tgScrapeMode = 'members';
        let waScrapeMode = 'members';
        let waChatFilter = 'all';    // all | personal | group | channel
        let tgcChatFilter = 'all';   // all | personal | group | channel
        let waChatCache = [];        // chat terakhir dari server (untuk filter/hitung tanpa refetch)
        let tgcChatCache = [];
        let waSearchTerm = '';
        let tgcSearchTerm = '';
        let waSelected = new Set();   // jid terpilih (multi-select)
        let tgcSelected = new Set();
        let waAttachPath = '';        // path media lampiran
        let tgcAttachPath = '';
        const EMOJI_SET = [0x1F600,0x1F601,0x1F602,0x1F923,0x1F60A,0x1F60D,0x1F618,0x1F60E,0x1F973,0x1F91D,0x1F44D,0x1F64F,0x1F525,0x2705,0x2764,0x1F4AF,0x1F389,0x1F6D2,0x1F4E6,0x1F4B8,0x26A1,0x2B50,0x1F622,0x1F62E,0x1F64C,0x1F44C,0x1F4AA,0x1F4E2,0x1F381,0x2728].map(cp => String.fromCodePoint(cp));

        function chatKind(c) {
            const jid = c.jid || c.id || '';
            if (c.kind) return c.kind;                 // Telegram sudah punya kind
            if (c.is_group || String(jid).endsWith('@g.us')) return 'group';
            if (String(jid).endsWith('@newsletter')) return 'channel';
            return 'personal';
        }

        function avatarHtml(name, kind) {
            const ch = (name || '?').trim().charAt(0).toUpperCase() || '?';
            const badge = kind === 'group' ? '<i class="fa-solid fa-users cav-badge"></i>'
                : kind === 'channel' ? '<i class="fa-solid fa-bullhorn cav-badge"></i>'
                : '<i class="fa-solid fa-user cav-badge"></i>';
            return `<span class="chat-avatar">${escapeHtml(ch)}${badge}</span>`;
        }

        function updateFilterCounts(barId, chats) {
            const counts = { all: chats.length, group: 0, channel: 0, personal: 0 };
            chats.forEach(c => { const k = chatKind(c); if (counts[k] !== undefined) counts[k]++; });
            document.querySelectorAll(`#${barId} .chat-filter-btn`).forEach(btn => {
                const el = btn.querySelector('.cf-count');
                if (el) el.textContent = counts[btn.dataset.filter] ?? 0;
            });
        }

        function buildEmojiPop(popId, inputId) {
            const pop = document.getElementById(popId);
            if (pop.dataset.built) return;
            pop.dataset.built = '1';
            EMOJI_SET.forEach(e => {
                const b = document.createElement('button');
                b.type = 'button'; b.className = 'emoji-cell'; b.textContent = e;
                b.onclick = () => {
                    const inp = document.getElementById(inputId);
                    inp.value += e; inp.focus();
                };
                pop.appendChild(b);
            });
        }

        async function pickMediaFile() {
            if (window.pywebview && window.pywebview.api && window.pywebview.api.pick_file) {
                try { return await window.pywebview.api.pick_file('media'); }
                catch (e) { return ''; }
            }
            return (prompt('Path file media (gambar/video/dokumen):') || '').trim();
        }

        function setBadge(el, on, onText, offText) {
            el.textContent = on ? onText : offText;
            el.className = 'sb-badge ' + (on ? 'sb-on' : 'sb-off');
        }

        function stopChatLogPolling() {
            // Hentikan semua polling tab Chat
            if (waTimer) { clearInterval(waTimer); waTimer = null; }
            if (waQrTimer) { clearInterval(waQrTimer); waQrTimer = null; }
            if (tgcTimer) { clearInterval(tgcTimer); tgcTimer = null; }
            if (tgcChatTimer) { clearInterval(tgcChatTimer); tgcChatTimer = null; }
        }

        function startChatSubPolling() {
            stopChatLogPolling();
            if (activeChatSub === 'wa') {
                refreshWaAll();
                waTimer = setInterval(refreshWaAll, 2000);   // near real-time
            } else if (activeChatSub === 'tg') {
                refreshTgcAll();
                // Status akun (/api/tg/status) HARUS connect+cek jaringan Telegram
                // per akun -- 26/27 Jul: polling ini tiap 2.5 detik terbukti bikin
                // event-loop TG kebanjiran (apalagi makin banyak akun / makin banyak
                // instance GAMA nyala bersamaan), sampai proses LAIN kayak login akun
                // baru ikut ke-antre lama & kelihatan "tidak bisa". Backend sudah cache
                // 20 detik, tapi polling di sini tetap dikurangi jauh biar gak sia-sia.
                tgcTimer = setInterval(refreshTgcAll, 15000);
                // Chat/pesan tetap near real-time -- ini cuma baca dialog Telegram yang
                // sudah connected, jauh lebih ringan drpd cek ulang status login.
                tgcChatTimer = setInterval(refreshTgcChatsLive, 3000);
            } else if (activeChatSub === 'blast') {
                refreshBlastPanel();
            }
        }

        // -- Klien WhatsApp --
        function refreshWaAll() {
            fetch('/api/wa/status').then(r => r.json()).then(s => {
                const box = document.getElementById('waUnavailable');
                if (!s.available) {
                    box.textContent = s.reason || 'Engine WhatsApp belum siap.';
                    box.classList.remove('view-hidden');
                    document.getElementById('waLayout').classList.add('view-hidden');
                    return;
                }
                box.classList.add('view-hidden');
                document.getElementById('waLayout').classList.remove('view-hidden');
                waSessionsCache = s.sessions || [];
                if (waActiveSession && !waSessionsCache.some(x => x.name === waActiveSession)) {
                    const mapped = waSessionsCache.find(x => (x.aliases || []).includes(waActiveSession));
                    if (mapped) waActiveSession = mapped.name;
                }
                renderWaSessions();
                if (waActiveSession) {
                    loadWaChats();
                    if (waActiveChat) loadWaMessages();
                }
            }).catch(() => {});
        }

        function waStatusLabel(st) {
            return { starting: 'menyiapkan', qr: 'scan QR', connected: 'terhubung',
                     disconnected: 'terputus', logged_out: 'logout', error: 'error' }[st] || st;
        }

        function renderWaSessions() {
            const sel = document.getElementById('waSessionSelect');
            const dot = document.getElementById('waSessionDot');
            const btn = document.getElementById('waSessionLogout');
            sel.innerHTML = '';
            if (!waSessionsCache.length) {
                sel.add(new Option('Belum ada akun - klik "+ Akun" lalu scan QR', ''));
                sel.disabled = true;
                btn.disabled = true;
                dot.className = 'wa-dot';
                return;
            }
            sel.disabled = false;
            btn.disabled = false;
            sel.add(new Option(`Pilih akun WA (${waSessionsCache.length})...`, ''));
            waSessionsCache.forEach(s => {
                sel.add(new Option(`${s.display || s.name} - ${waStatusLabel(s.status)}`, s.name));
            });
            if (waActiveSession && waSessionsCache.some(x => x.name === waActiveSession)) {
                sel.value = waActiveSession;
            }
            const cur = waSessionsCache.find(x => x.name === sel.value);
            dot.className = 'wa-dot' + (cur ? ' ' + cur.status : '');

            sel.onchange = () => {
                const s = waSessionsCache.find(x => x.name === sel.value);
                if (!s) return;
                waActiveSession = s.name;
                waActiveChat = null;
                document.getElementById('waChatHeader').innerHTML = '<i class="fa-brands fa-whatsapp"></i> <span>Pilih chat di sebelah kiri</span>';
                setWaComposerEnabled(false);
                renderWaSessions();
                if (s.status === 'qr') startWaQrPolling(s.name);
                loadWaChats();
            };
            btn.onclick = () => {
                const s = waSessionsCache.find(x => x.name === sel.value) ||
                          waSessionsCache.find(x => x.name === waActiveSession);
                if (!s) { showToast('Pilih akun WA dulu', 'error'); return; }
                if (!confirm(`Logout akun WA "${s.name}"? Sesi login akan dihapus dari WhatsApp.`)) return;
                api(`/api/wa/sessions/${encodeURIComponent(s.name)}/logout`, 'POST').then(d => {
                    if (d.success) { showToast('Akun WA logout', 'info'); if (waActiveSession === s.name) waActiveSession = null; refreshWaAll(); }
                    else showToast(d.error || 'Gagal logout', 'error');
                }).catch(() => showToast('Gagal terhubung ke server', 'error'));
            };
        }

        function startWaQrPolling(name) {
            if (waQrTimer) clearInterval(waQrTimer);
            const qrBox = document.getElementById('waQrBox');
            const qrImg = document.getElementById('waQrImg');
            const tick = () => {
                fetch(`/api/wa/sessions/${encodeURIComponent(name)}/qr`).then(r => r.json()).then(d => {
                    if (d.status === 'qr' && d.qr) {
                        qrImg.src = d.qr;
                        qrBox.classList.remove('view-hidden');
                    } else {
                        qrBox.classList.add('view-hidden');
                        if (d.status === 'connected') {
                            clearInterval(waQrTimer); waQrTimer = null;
                            showToast(`Akun WA "${name}" terhubung!`, 'success');
                            refreshWaAll();
                        } else if (d.status === 'error') {
                            clearInterval(waQrTimer); waQrTimer = null;
                            showToast('Gagal login WA: ' + (d.error || ''), 'error');
                        }
                    }
                }).catch(() => {});
            };
            tick();
            waQrTimer = setInterval(tick, 2000);
        }

        function loadWaChats() {
            if (!waActiveSession) return;
            fetch(`/api/wa/chats/${encodeURIComponent(waActiveSession)}`).then(r => r.json()).then(d => {
                waChatCache = d.chats || [];
                renderWaChats();
            }).catch(() => {});
        }

        function renderWaChats() {
            const list = document.getElementById('waChatList');
            updateFilterCounts('waChatFilter', waChatCache);
            let chats = waChatCache.filter(c => waChatFilter === 'all' || chatKind(c) === waChatFilter);
            if (waSearchTerm) chats = chats.filter(c => [c.name || '', c.jid || '', c.last_text || ''].join(' ').toLowerCase().includes(waSearchTerm));
            list.innerHTML = '';
            if (!chats.length) {
                list.innerHTML = '<p class="sb-muted wa-pad">Belum ada chat di kategori ini. Riwayat tersinkron otomatis setelah link (butuh beberapa saat), atau mulai lewat tombol chat baru.</p>';
                updateWaSelBar();
                return;
            }
            chats.forEach(c => {
                const kind = chatKind(c);
                const el = document.createElement('div');
                el.className = 'wa-chat-item' + (waActiveChat && waActiveChat.jid === c.jid ? ' active' : '');
                const time = c.last_ts ? new Date(c.last_ts * 1000).toLocaleTimeString('id-ID', { hour: '2-digit', minute: '2-digit' }) : '';
                el.innerHTML = `
                    <input type="checkbox" class="chat-sel" ${waSelected.has(c.jid) ? 'checked' : ''}>
                    ${avatarHtml(c.name, kind)}
                    <div class="chat-item-body">
                        <div class="wa-chat-top">
                            <span class="wa-chat-name"><span></span></span>
                            <span class="wa-chat-time">${escapeHtml(time)}</span>
                        </div>
                        <div class="wa-chat-bottom">
                            <span class="wa-chat-preview"></span>
                            <span class="wa-chat-right">
                                ${c.unread ? `<span class="wa-unread">${c.unread}</span>` : ''}
                                <button type="button" class="chat-del-btn" title="Hapus chat dari GAMA"><i class="fa-solid fa-trash-can"></i></button>
                            </span>
                        </div>
                    </div>
                `;
                el.querySelector('.wa-chat-name span').textContent = c.name;
                el.querySelector('.wa-chat-preview').textContent = c.last_text || '';
                el.onclick = () => selectWaChat(c);
                const cb = el.querySelector('.chat-sel');
                cb.onclick = (e) => {
                    e.stopPropagation();
                    if (cb.checked) waSelected.add(c.jid); else waSelected.delete(c.jid);
                    updateWaSelBar();
                };
                el.querySelector('.chat-del-btn').onclick = (e) => {
                    e.stopPropagation();
                    if (!confirm(`Hapus chat "${c.name}"?\nChat akan dihapus dari GAMA DAN dari WhatsApp di HP (tersinkron).`)) return;
                    api('/api/wa/delete_chat', 'POST', { session: waActiveSession, jid: c.jid }).then(res => {
                        if (res.success) {
                            waSelected.delete(c.jid);
                            if (waActiveChat && waActiveChat.jid === c.jid) { waActiveChat = null; resetWaChatArea(); }
                            showToast(res.synced ? 'Chat dihapus (tersinkron ke HP)' : 'Chat dihapus dari GAMA (sinkron HP gagal: ' + (res.sync_error || '') + ')', res.synced ? 'success' : 'info');
                            loadWaChats();
                        } else showToast(res.error || 'Gagal menghapus', 'error');
                    }).catch(() => showToast('Gagal terhubung ke server', 'error'));
                };
                list.appendChild(el);
            });
            updateWaSelBar();
        }

        function updateWaSelBar() {
            const n = waSelected.size;
            document.getElementById('waSelCount').textContent = n;
            document.getElementById('waBulkDelete').classList.toggle('view-hidden', n === 0);
        }

        function bulkDeleteWa() {
            const jids = [...waSelected];
            if (!jids.length) return;
            if (!confirm(`Hapus ${jids.length} chat terpilih?\nDihapus dari GAMA. Sinkron ke WhatsApp di HP dicoba otomatis, tapi tidak selalu berhasil (keterbatasan WhatsApp) - kalau gagal, chat tetap dihapus di GAMA saja.`)) return;
            Promise.all(jids.map(jid =>
                api('/api/wa/delete_chat', 'POST', { session: waActiveSession, jid })
                    .catch(() => ({ error: 'Gagal terhubung ke server' }))
            )).then(results => {
                let okCount = 0, syncedCount = 0;
                results.forEach((res, i) => {
                    if (res && res.success) {
                        okCount++;
                        if (res.synced) syncedCount++;
                        waSelected.delete(jids[i]);
                        if (waActiveChat && waActiveChat.jid === jids[i]) { waActiveChat = null; resetWaChatArea(); }
                    }
                });
                const failed = jids.length - okCount;
                document.getElementById('waSelectAll').checked = false;
                if (failed) {
                    showToast(`${okCount} chat dihapus (${syncedCount} tersinkron ke HP), ${failed} GAGAL dihapus`, failed === jids.length ? 'error' : 'info');
                } else {
                    showToast(`${okCount} chat dihapus dari GAMA (${syncedCount} tersinkron ke HP, ${okCount - syncedCount} lokal saja)`, 'info');
                }
                loadWaChats();
            });
        }

        function resetWaChatArea() {
            document.getElementById('waChatHeader').innerHTML = '<i class="fa-brands fa-whatsapp"></i> <span>Pilih chat di sebelah kiri</span>';
            document.getElementById('waMessages').innerHTML = '<p class="sb-muted wa-empty-hint">Pilih chat untuk melihat pesan.</p>';
            setWaComposerEnabled(false);
        }

        function setWaComposerEnabled(on) {
            document.getElementById('waComposerInput').disabled = !on;
            document.getElementById('waSendBtn').disabled = !on;
        }

        function selectWaChat(c) {
            waActiveChat = { jid: c.jid, name: c.name };
            const header = document.getElementById('waChatHeader');
            header.innerHTML = `<i class="fa-brands fa-whatsapp"></i> <span></span> <span class="sb-muted wa-jid"></span>`;
            header.querySelector('span').textContent = c.name;
            header.querySelector('.wa-jid').textContent = c.jid;
            setWaComposerEnabled(true);
            api('/api/wa/read', 'POST', { session: waActiveSession, jid: c.jid }).catch(() => {});
            loadWaMessages(true);
            loadWaChats();
        }

        function loadWaMessages(scrollBottom) {
            if (!waActiveSession || !waActiveChat) return;
            fetch(`/api/wa/messages/${encodeURIComponent(waActiveSession)}?jid=${encodeURIComponent(waActiveChat.jid)}&limit=80`)
                .then(r => r.json()).then(d => {
                    const box = document.getElementById('waMessages');
                    const msgs = d.messages || [];
                    const stick = scrollBottom || (box.scrollTop + box.clientHeight >= box.scrollHeight - 30);
                    box.innerHTML = '';
                    if (!msgs.length) {
                        box.innerHTML = '<p class="sb-muted wa-empty-hint">Belum ada pesan di chat ini. Kirim pesan pertama lewat kolom di bawah.</p>';
                        return;
                    }
                    msgs.forEach(m => {
                        const b = document.createElement('div');
                        b.className = 'wa-bubble ' + (m.from_me ? 'wa-out' : 'wa-in');
                        const time = m.ts ? new Date(m.ts * 1000).toLocaleTimeString('id-ID', { hour: '2-digit', minute: '2-digit' }) : '';
                        b.innerHTML = `<span class="wa-bubble-text"></span><span class="wa-bubble-ts">${escapeHtml(time)}</span>`;
                        b.querySelector('.wa-bubble-text').innerHTML = linkifyText(m.text || (m.media ? `[${m.media}]` : ''));
                        box.appendChild(b);
                    });
                    if (stick) box.scrollTop = box.scrollHeight;
                }).catch(() => {});
        }

        function sendWaMessage() {
            const input = document.getElementById('waComposerInput');
            const text = input.value.trim();
            if ((!text && !waAttachPath) || !waActiveSession || !waActiveChat) return;
            const media = waAttachPath;
            input.value = '';
            clearWaAttach();
            api('/api/wa/send', 'POST', { session: waActiveSession, jid: waActiveChat.jid, text, media_path: media }).then(d => {
                if (d.success) { loadWaMessages(true); loadWaChats(); }
                else { showToast(d.error || 'Gagal mengirim', 'error'); input.value = text; }
            }).catch(() => { showToast('Gagal terhubung ke server', 'error'); input.value = text; });
        }

        function clearWaAttach() {
            waAttachPath = '';
            document.getElementById('waAttachBar').classList.add('view-hidden');
        }

        // -- Klien Telegram (chat) --
        function refreshTgcAll() {
            fetch('/api/tg/status').then(r => r.json()).then(s => {
                const box = document.getElementById('tgcUnavailable');
                if (!s.available) {
                    box.textContent = s.reason || 'Engine Telegram belum siap.';
                    box.classList.remove('view-hidden');
                    document.getElementById('tgcLayout').classList.add('view-hidden');
                    return;
                }
                box.classList.add('view-hidden');
                document.getElementById('tgcLayout').classList.remove('view-hidden');
                tgSessionsCache = s.sessions || [];
                renderTgcSessions();
                refreshTgcChatsLive();
            }).catch(() => {});
        }

        function refreshTgcChatsLive() {
            // Bagian near-real-time (chat/pesan) DIPISAH dari refreshTgcAll --
            // ini tidak perlu cek ulang status login ke Telegram, cuma baca
            // dialog dari client yang sudah connected, jadi aman di-poll sering.
            if (!tgcActiveSession) return;
            loadTgcChats();
            if (tgcActiveChat) loadTgcMessages();
        }

        function renderTgcSessions() {
            const sel = document.getElementById('tgcSessionSelect');
            const dot = document.getElementById('tgcSessionDot');
            const btn = document.getElementById('tgcSessionLogout');
            sel.innerHTML = '';
            const authed = tgSessionsCache.filter(x => x.authorized);
            if (!authed.length) {
                sel.add(new Option('Belum ada akun - klik "+ Akun" untuk login', ''));
                sel.disabled = true;
                btn.disabled = true;
                dot.className = 'wa-dot';
                return;
            }
            sel.disabled = false;
            btn.disabled = false;
            sel.add(new Option(`Pilih akun Telegram (${authed.length})...`, ''));
            authed.forEach(s => sel.add(new Option(s.name, s.name)));
            if (tgcActiveSession && authed.some(x => x.name === tgcActiveSession)) {
                sel.value = tgcActiveSession;
            }
            dot.className = 'wa-dot' + (sel.value ? ' connected' : '');

            sel.onchange = () => {
                const s = authed.find(x => x.name === sel.value);
                if (!s) return;
                tgcActiveSession = s.name;
                tgcActiveChat = null;
                document.getElementById('tgcChatHeader').innerHTML = '<i class="fa-brands fa-telegram"></i> <span>Pilih chat di sebelah kiri</span>';
                setTgcComposerEnabled(false);
                renderTgcSessions();
                loadTgcChats();
            };
            btn.onclick = () => {
                const s = authed.find(x => x.name === sel.value) ||
                          authed.find(x => x.name === tgcActiveSession);
                if (!s) { showToast('Pilih akun Telegram dulu', 'error'); return; }
                if (!confirm(`Logout akun Telegram "${s.name}"?`)) return;
                api('/api/tg/logout', 'POST', { name: s.name }).then(() => {
                    if (tgcActiveSession === s.name) tgcActiveSession = null;
                    showToast('Akun Telegram logout', 'info');
                    refreshTgcAll();
                });
            };
        }

        function loadTgcChats() {
            if (!tgcActiveSession) return;
            fetch(`/api/tg/chats/${encodeURIComponent(tgcActiveSession)}`).then(r => r.json()).then(d => {
                if (d.error) { document.getElementById('tgcChatList').innerHTML = `<p class="sb-muted wa-pad">${escapeHtml(d.error)}</p>`; return; }
                // Seragamkan kind: Telethon pakai 'user' -> 'personal'
                tgcChatCache = (d.chats || []).map(c => ({ ...c, kind: c.kind === 'user' ? 'personal' : c.kind }));
                renderTgcChats();
            }).catch(() => {});
        }

        function renderTgcChats() {
            const list = document.getElementById('tgcChatList');
            updateFilterCounts('tgcChatFilter', tgcChatCache);
            let chats = tgcChatCache.filter(c => tgcChatFilter === 'all' || c.kind === tgcChatFilter);
            if (tgcSearchTerm) chats = chats.filter(c => (c.name || c.username || '').toLowerCase().includes(tgcSearchTerm));
            list.innerHTML = '';
            if (!chats.length) { list.innerHTML = '<p class="sb-muted wa-pad">Tidak ada chat di kategori ini.</p>'; updateTgcSelBar(); return; }
            chats.forEach(c => {
                const el = document.createElement('div');
                el.className = 'wa-chat-item' + (tgcActiveChat && tgcActiveChat.id === c.id ? ' active' : '');
                const time = c.last_ts ? new Date(c.last_ts * 1000).toLocaleTimeString('id-ID', { hour: '2-digit', minute: '2-digit' }) : '';
                el.innerHTML = `
                    <input type="checkbox" class="chat-sel" ${tgcSelected.has(c.id) ? 'checked' : ''}>
                    ${avatarHtml(c.name || c.username || c.id, c.kind)}
                    <div class="chat-item-body">
                        <div class="wa-chat-top">
                            <span class="wa-chat-name"><span></span></span>
                            <span class="wa-chat-time">${escapeHtml(time)}</span>
                        </div>
                        <div class="wa-chat-bottom">
                            <span class="wa-chat-preview"></span>
                            <span class="wa-chat-right">
                                ${c.unread ? `<span class="wa-unread">${c.unread}</span>` : ''}
                                <button type="button" class="chat-del-btn" title="Hapus chat (Delete chat Telegram)"><i class="fa-solid fa-trash-can"></i></button>
                            </span>
                        </div>
                    </div>
                `;
                el.querySelector('.wa-chat-name span').textContent = c.name || c.username || c.id;
                el.querySelector('.wa-chat-preview').textContent = c.last_text || '';
                el.onclick = () => selectTgcChat(c);
                const cb = el.querySelector('.chat-sel');
                cb.onclick = (e) => {
                    e.stopPropagation();
                    if (cb.checked) tgcSelected.add(c.id); else tgcSelected.delete(c.id);
                    updateTgcSelBar();
                };
                el.querySelector('.chat-del-btn').onclick = (e) => {
                    e.stopPropagation();
                    const kindLbl = c.kind === 'personal' ? 'hapus riwayat untukmu' : 'keluar dari grup/channel';
                    if (!confirm(`Hapus chat "${c.name || c.id}" di Telegram?\nIni sama seperti "Delete chat" di app Telegram (${kindLbl}).`)) return;
                    const peer = c.username ? '@' + c.username : c.id;
                    api('/api/tg/delete_chat', 'POST', { name: tgcActiveSession, peer }).then(res => {
                        if (res.success) {
                            tgcSelected.delete(c.id);
                            if (tgcActiveChat && tgcActiveChat.id === c.id) { tgcActiveChat = null; resetTgcChatArea(); }
                            showToast('Chat dihapus', 'info');
                            loadTgcChats();
                        } else showToast(res.error || 'Gagal menghapus', 'error');
                    }).catch(() => showToast('Gagal terhubung ke server', 'error'));
                };
                list.appendChild(el);
            });
            updateTgcSelBar();
        }

        function updateTgcSelBar() {
            const n = tgcSelected.size;
            document.getElementById('tgcSelCount').textContent = n;
            document.getElementById('tgcBulkDelete').classList.toggle('view-hidden', n === 0);
        }

        function bulkDeleteTgc() {
            const ids = [...tgcSelected];
            if (!ids.length) return;
            if (!confirm(`Hapus ${ids.length} chat terpilih di Telegram? (sama seperti Delete chat di app)`)) return;
            const byId = Object.fromEntries(tgcChatCache.map(c => [c.id, c]));
            Promise.all(ids.map(id => {
                const c = byId[id];
                if (!c) return Promise.resolve({ error: 'Chat tidak ditemukan di cache' });
                const peer = c.username ? '@' + c.username : c.id;
                return api('/api/tg/delete_chat', 'POST', { name: tgcActiveSession, peer })
                    .catch(() => ({ error: 'Gagal terhubung ke server' }));
            })).then(results => {
                let okCount = 0;
                const failedNames = [];
                results.forEach((res, i) => {
                    const c = byId[ids[i]];
                    if (res && res.success) {
                        okCount++;
                        tgcSelected.delete(ids[i]);
                        if (tgcActiveChat && tgcActiveChat.id === ids[i]) { tgcActiveChat = null; resetTgcChatArea(); }
                    } else {
                        failedNames.push((c && (c.name || c.id)) || ids[i]);
                    }
                });
                document.getElementById('tgcSelectAll').checked = false;
                if (failedNames.length) {
                    showToast(`${okCount} chat dihapus, ${failedNames.length} GAGAL (${failedNames.slice(0, 3).join(', ')}${failedNames.length > 3 ? ', ...' : ''})`,
                        failedNames.length === ids.length ? 'error' : 'info');
                } else {
                    showToast(`${okCount} chat dihapus`, 'info');
                }
                loadTgcChats();
            });
        }

        function resetTgcChatArea() {
            document.getElementById('tgcChatHeader').innerHTML = '<i class="fa-brands fa-telegram"></i> <span>Pilih chat di sebelah kiri</span>';
            document.getElementById('tgcMessages').innerHTML = '<p class="sb-muted wa-empty-hint">Pilih chat untuk melihat pesan.</p>';
            setTgcComposerEnabled(false);
            tgcActiveTopicId = null;
            tgcTopicsCache = [];
            document.getElementById('tgcTopicTabs').classList.add('view-hidden');
        }

        function setTgcComposerEnabled(on) {
            document.getElementById('tgcComposerInput').disabled = !on;
            document.getElementById('tgcSendBtn').disabled = !on;
        }

        function selectTgcChat(c) {
            const peer = c.username ? '@' + c.username : c.id;
            tgcActiveChat = { id: c.id, name: c.name || c.username || c.id, peer };
            const header = document.getElementById('tgcChatHeader');
            header.innerHTML = `<i class="fa-brands fa-telegram"></i> <span></span>`;
            header.querySelector('span').textContent = tgcActiveChat.name;
            setTgcComposerEnabled(true);
            api('/api/tg/read', 'POST', { name: tgcActiveSession, peer }).catch(() => {});
            const tabsBox = document.getElementById('tgcTopicTabs');
            if (c.is_forum) {
                tabsBox.classList.remove('view-hidden');
                tabsBox.innerHTML = '<span class="sb-muted" style="font-size:0.8rem;">Memuat topics...</span>';
                fetch(`/api/tg/topics/${encodeURIComponent(tgcActiveSession)}?target=${encodeURIComponent(peer)}`)
                    .then(r => r.json()).then(d => {
                        tgcTopicsCache = (d.is_forum && d.topics) ? d.topics : [];
                        if (!tgcTopicsCache.length) {
                            tabsBox.classList.add('view-hidden');
                            tgcActiveTopicId = null;
                            loadTgcMessages(true);
                            return;
                        }
                        // Default buka topic pertama (persis kayak Telegram Web/App).
                        tgcActiveTopicId = tgcTopicsCache[0].id;
                        renderTgcTopicTabs();
                        loadTgcMessages(true);
                    }).catch(() => { tabsBox.classList.add('view-hidden'); tgcActiveTopicId = null; loadTgcMessages(true); });
            } else {
                tabsBox.classList.add('view-hidden');
                tgcActiveTopicId = null;
                tgcTopicsCache = [];
                loadTgcMessages(true);
            }
            loadTgcChats();
        }

        function renderTgcTopicTabs() {
            const tabsBox = document.getElementById('tgcTopicTabs');
            tabsBox.innerHTML = '';
            tgcTopicsCache.forEach(t => {
                const tab = document.createElement('button');
                tab.type = 'button';
                tab.className = 'tg-topic-tab' + (t.id === tgcActiveTopicId ? ' active' : '');
                tab.innerHTML = `<span class="tg-topic-dot" style="background:${escapeHtml(t.icon_color || '#6FB9F0')}"></span><span></span>`;
                tab.querySelector('span:last-child').textContent = t.title;
                tab.onclick = () => {
                    if (tgcActiveTopicId === t.id) return;
                    tgcActiveTopicId = t.id;
                    renderTgcTopicTabs();
                    loadTgcMessages(true);
                };
                tabsBox.appendChild(tab);
            });
        }

        function loadTgcMessages(scrollBottom) {
            if (!tgcActiveSession || !tgcActiveChat) return;
            let url = `/api/tg/messages/${encodeURIComponent(tgcActiveSession)}?peer=${encodeURIComponent(tgcActiveChat.peer)}&limit=60`;
            if (tgcActiveTopicId) url += `&topic_id=${encodeURIComponent(tgcActiveTopicId)}`;
            fetch(url)
                .then(r => r.json()).then(d => {
                    const box = document.getElementById('tgcMessages');
                    if (d.error) { box.innerHTML = `<p class="sb-muted wa-empty-hint">${escapeHtml(d.error)}</p>`; return; }
                    const msgs = d.messages || [];
                    const stick = scrollBottom || (box.scrollTop + box.clientHeight >= box.scrollHeight - 30);
                    box.innerHTML = '';
                    if (!msgs.length) { box.innerHTML = '<p class="sb-muted wa-empty-hint">Belum ada pesan.</p>'; return; }
                    msgs.forEach(m => {
                        const b = document.createElement('div');
                        b.className = 'wa-bubble wa-tg ' + (m.from_me ? 'wa-out' : 'wa-in');
                        const time = m.ts ? new Date(m.ts * 1000).toLocaleTimeString('id-ID', { hour: '2-digit', minute: '2-digit' }) : '';
                        const senderTag = (!m.from_me && m.sender) ? `<span class="wa-bubble-sender"></span>` : '';
                        b.innerHTML = `${senderTag}<span class="wa-bubble-text"></span><span class="wa-bubble-ts">${escapeHtml(time)}</span>`;
                        if (senderTag) b.querySelector('.wa-bubble-sender').textContent = m.sender;
                        b.querySelector('.wa-bubble-text').innerHTML = linkifyText(m.text || '');
                        box.appendChild(b);
                    });
                    if (stick) box.scrollTop = box.scrollHeight;
                }).catch(() => {});
        }

        function sendTgcMessage() {
            const input = document.getElementById('tgcComposerInput');
            const text = input.value.trim();
            if ((!text && !tgcAttachPath) || !tgcActiveSession || !tgcActiveChat) return;
            const media = tgcAttachPath;
            input.value = '';
            clearTgcAttach();
            const payload = { name: tgcActiveSession, peer: tgcActiveChat.peer, text, media_path: media };
            if (tgcActiveTopicId) payload.topic_id = tgcActiveTopicId;
            api('/api/tg/send', 'POST', payload).then(d => {
                if (d.success) { loadTgcMessages(true); loadTgcChats(); }
                else { showToast(d.error || 'Gagal mengirim', 'error'); input.value = text; }
            }).catch(() => { showToast('Gagal terhubung ke server', 'error'); input.value = text; });
        }

        function clearTgcAttach() {
            tgcAttachPath = '';
            document.getElementById('tgcAttachBar').classList.add('view-hidden');
        }

        function tgcStartLogin(onDone) {
            const name = prompt('Nama sesi Telegram (bebas, mis. "akun1"):');
            if (!name || !name.trim()) return;
            const phone = prompt('Nomor HP Telegram (format internasional, mis. +6281234567890):');
            if (!phone || !phone.trim()) return;
            showToast('Mengirim kode ke Telegram...', 'info');
            api('/api/tg/login', 'POST', { name: name.trim(), phone: phone.trim() }).then(d => {
                if (d.error) { showToast(d.error, 'error'); return; }
                if (d.already) { showToast('Akun sudah login', 'success'); if (onDone) onDone(name.trim()); return; }
                const code = prompt('Masukkan kode login dari Telegram (cek app / SMS):');
                if (!code) return;
                api('/api/tg/verify_code', 'POST', { name: name.trim(), code: code.trim() }).then(r => {
                    if (r.need_2fa) {
                        const pw = prompt('Akun ini pakai verifikasi 2 langkah. Masukkan password 2FA:');
                        if (!pw) return;
                        api('/api/tg/verify_2fa', 'POST', { name: name.trim(), password: pw }).then(r2 => {
                            if (r2.success) { showToast('Login Telegram berhasil: ' + (r2.me || ''), 'success'); if (onDone) onDone(name.trim()); }
                            else showToast(r2.error || 'Gagal 2FA', 'error');
                        });
                    } else if (r.success) {
                        showToast('Login Telegram berhasil: ' + (r.me || ''), 'success');
                        if (onDone) onDone(name.trim());
                    } else showToast(r.error || 'Kode salah', 'error');
                });
            }).catch(() => showToast('Gagal terhubung ke server', 'error'));
        }

        // -- Scrape & Blast --
        let scrapePollTimer = null;
        let blastPollTimer = null;
        let blastTemplatesCache = [];
        let blastGroupListsCache = [];
        let blastAvailableGroups = [];
        let selectedBlastGroups = [];

        function refreshBlastPanel() {
            // Status engine WA & TG + isi dropdown sesi
            fetch('/api/wa/status').then(r => r.json()).then(s => {
                setBadge(document.getElementById('waBadge'), s.available, 'ENGINE SIAP', 'BELUM TERSEDIA');
                if (!s.available) document.getElementById('waReason').textContent = s.reason || '';
                else document.getElementById('waReason').textContent = 'Login akun WA di tab "WhatsApp Chat" dulu.';
                waSessionsCache = s.sessions || [];
                const conn = waSessionsCache.filter(x => x.status === 'connected');
                fillSelect('waBlastSession', conn.map(x => ({ v: x.name, t: x.name })), 'Belum ada sesi WA terhubung');
                updateBlastSessionOptions();
            }).catch(() => {});
            fetch('/api/tg/status').then(r => r.json()).then(s => {
                setBadge(document.getElementById('tgBadge'), s.available, 'ENGINE SIAP', 'BELUM TERSEDIA');
                document.getElementById('tgReason').textContent = s.available ? '' : (s.reason || '');
                tgSessionsCache = s.sessions || [];
                const authed = tgSessionsCache.filter(x => x.authorized);
                fillSelect('tgSessionSel', authed.map(x => ({ v: x.name, t: x.name })), 'Belum ada akun Telegram');
            }).catch(() => {});
            refreshScrapeFiles();
            refreshBlastTemplates();
            refreshBlastGroupLists();
            updateBlastSessionOptions();
            updateBlastTargetInfo();
        }

        function fillSelect(id, items, emptyText) {
            const sel = document.getElementById(id);
            if (!sel) return;
            const prev = sel.value;
            sel.innerHTML = '';
            if (!items.length) {
                sel.add(new Option(emptyText || '(kosong)', ''));
            } else {
                items.forEach(it => sel.add(new Option(it.t, it.v)));
                if (items.some(it => it.v === prev)) sel.value = prev;
            }
        }

        function updateBlastSessionOptions() {
            const platform = document.getElementById('blastPlatform').value;
            const src = platform === 'telegram'
                ? tgSessionsCache.filter(x => x.authorized).map(x => ({ v: x.name, t: x.name }))
                : waSessionsCache.filter(x => x.status === 'connected').map(x => ({ v: x.name, t: x.name }));
            fillSelect('blastSessionSel', src, platform === 'telegram' ? 'Belum ada akun Telegram' : 'Belum ada sesi WA terhubung');
            refreshBlastTemplates();
            refreshBlastGroupLists();
            blastAvailableGroups = [];
            fillSelect('blastGroupSel', [], 'Klik Muat Grup');
        }

        function refreshScrapeFiles() {
            fetch('/api/chatgrow/files').then(r => r.json()).then(d => {
                const list = document.getElementById('scrapeFileList');
                const files = d.files || [];
                list.innerHTML = '';
                if (!files.length) {
                    list.innerHTML = '<p class="sb-muted">Belum ada data scrape.</p>';
                    selectedScrapeFile = '';
                    updateBlastTargetInfo();
                    return;
                }
                files.forEach(f => {
                    const el = document.createElement('div');
                    el.className = 'scrape-file-item' + (selectedScrapeFile === f.file ? ' active' : '');
                    const dt = new Date(f.mtime * 1000).toLocaleString('id-ID', { dateStyle: 'short', timeStyle: 'short' });
                    el.innerHTML = `
                        <i class="fa-solid fa-file-csv"></i>
                        <div class="scrape-file-main">
                            <span class="scrape-file-name"></span>
                            <span class="scrape-file-meta">${f.rows} kontak - ${escapeHtml(dt)}</span>
                        </div>
                        <button type="button" class="icon-btn scrape-file-del" title="Hapus file CSV ini"><i class="fa-solid fa-trash"></i></button>
                    `;
                    el.querySelector('.scrape-file-name').textContent = f.file;
                    el.onclick = () => {
                        selectedScrapeFile = (selectedScrapeFile === f.file) ? '' : f.file;
                        refreshScrapeFiles();
                        updateBlastTargetInfo();
                    };
                    el.querySelector('.scrape-file-del').onclick = (e) => {
                        e.stopPropagation();
                        if (!confirm(`Hapus file scrape "${f.file}" dari daftar?`)) return;
                        api('/api/chatgrow/files', 'DELETE', { file: f.file }).then(res => {
                            if (!res.success) { showToast('Gagal menghapus file scrape', 'error'); return; }
                            if (selectedScrapeFile === f.file) selectedScrapeFile = '';
                            showToast('File scrape dihapus', 'info');
                            refreshScrapeFiles();
                            updateBlastTargetInfo();
                        }).catch(() => showToast('Gagal terhubung ke server', 'error'));
                    };
                    list.appendChild(el);
                });
            }).catch(() => {});
        }

        function updateBlastTargetInfo() {
            const info = document.getElementById('blastTargetInfo');
            const csvPath = document.getElementById('blastCsvPath')?.value.trim() || '';
            const manual = document.getElementById('blastManual')?.value.split('\n').map(s => s.trim()).filter(Boolean).length || 0;
            const start = document.getElementById('blastPersonalStart')?.value.trim() || '';
            const end = document.getElementById('blastPersonalEnd')?.value.trim() || '';
            const parts = [];
            if (selectedScrapeFile) parts.push('file scrape: <b>' + escapeHtml(selectedScrapeFile) + '</b>');
            if (csvPath) parts.push('CSV custom: <b>' + escapeHtml(csvPath) + '</b>');
            if (manual) parts.push('manual: <b>' + manual + '</b> target');
            if (selectedBlastGroups.length) parts.push('grup aktif: <b>' + selectedBlastGroups.length + '</b>');
            if (start || end) parts.push('range personal: <b>' + escapeHtml(start || '1') + '</b> s/d <b>' + escapeHtml(end || 'akhir') + '</b>');
            info.innerHTML = parts.length ? parts.join(' - ') : 'Pilih file scrape, CSV custom, target manual, atau grup yang ingin diblast.';
        }

        function refreshBlastTemplates() {
            const platform = document.getElementById('blastPlatform')?.value || 'whatsapp';
            fetch(`/api/chatgrow/blast_templates?platform=${encodeURIComponent(platform)}`).then(r => r.json()).then(d => {
                blastTemplatesCache = d.templates || [];
                fillSelect('blastTemplateSel', blastTemplatesCache.map(t => ({ v: t.name, t: t.name })), 'Belum ada template blast');
            }).catch(() => {});
        }

        function refreshBlastGroupLists() {
            const platform = document.getElementById('blastPlatform')?.value || 'whatsapp';
            fetch(`/api/chatgrow/group_lists?platform=${encodeURIComponent(platform)}`).then(r => r.json()).then(d => {
                blastGroupListsCache = d.group_lists || [];
                fillSelect('blastGroupListSel', blastGroupListsCache.map(g => ({ v: g.name, t: `${g.name} (${(g.groups || []).length})` })), 'Belum ada list grup');
            }).catch(() => {});
        }

        function normalizeBlastGroups(groups) {
            const map = new Map();
            (groups || []).forEach(g => {
                const target = String(g.target || '').trim();
                if (!target) return;
                map.set(target, { target, label: String(g.label || target).trim() || target });
            });
            return [...map.values()];
        }

        function renderSelectedBlastGroups() {
            const wrap = document.getElementById('selectedBlastGroups');
            if (!wrap) return;
            wrap.innerHTML = '';
            if (!selectedBlastGroups.length) {
                wrap.innerHTML = '<p class="sb-muted">Belum ada grup yang dipilih untuk blast.</p>';
                updateBlastTargetInfo();
                return;
            }
            selectedBlastGroups.forEach((g, idx) => {
                const chip = document.createElement('button');
                chip.type = 'button';
                chip.className = 'selected-group-chip';
                chip.innerHTML = `<span>${escapeHtml(g.label || g.target)}</span><i class="fa-solid fa-xmark"></i>`;
                chip.onclick = () => {
                    selectedBlastGroups.splice(idx, 1);
                    renderSelectedBlastGroups();
                };
                wrap.appendChild(chip);
            });
            updateBlastTargetInfo();
        }

        function loadBlastGroups() {
            const platform = document.getElementById('blastPlatform').value;
            const session = document.getElementById('blastSessionSel').value;
            if (!session) { showToast('Pilih sesi pengirim dulu', 'error'); return; }
            if (platform === 'telegram') {
                fetch(`/api/tg/dialogs/${encodeURIComponent(session)}`).then(r => r.json()).then(d => {
                    if (d.error) { showToast(d.error, 'error'); return; }
                    blastAvailableGroups = (d.dialogs || []).map(g => ({ target: g.username ? '@' + g.username : g.id, label: `${g.title || g.username || g.id}${g.participants ? ` (${g.participants})` : ''}` }));
                    fillSelect('blastGroupSel', blastAvailableGroups.map(g => ({ v: g.target, t: g.label })), 'Tidak ada grup/channel');
                    showToast(`${blastAvailableGroups.length} grup/channel dimuat`, 'success');
                }).catch(() => showToast('Gagal memuat grup Telegram', 'error'));
            } else {
                fetch(`/api/wa/groups/${encodeURIComponent(session)}`).then(r => r.json()).then(d => {
                    if (d.error) { showToast(d.error, 'error'); return; }
                    blastAvailableGroups = (d.groups || []).map(g => ({ target: g.jid, label: `${g.name} (${g.participants})`, open: g.open !== false }));
                    fillSelect('blastGroupSel', blastAvailableGroups.map(g => ({ v: g.target, t: g.label })), 'Tidak ada grup');
                    showToast(`${blastAvailableGroups.length} grup dimuat (semua)`, 'success');
                }).catch(() => showToast('Gagal memuat grup WA', 'error'));
            }
        }

        function filterPublicBlastGroups() {
            if (!blastAvailableGroups.length) { showToast('Muat grup dulu', 'error'); return; }
            const platform = document.getElementById('blastPlatform').value;
            if (platform === 'telegram') { showToast('Filter grup umum khusus WhatsApp', 'info'); return; }
            const pub = blastAvailableGroups.filter(g => g.open !== false);
            fillSelect('blastGroupSel', pub.map(g => ({ v: g.target, t: g.label })), 'Tidak ada grup umum');
            const hidden = blastAvailableGroups.length - pub.length;
            showToast(`${pub.length} grup umum (bisa dikirimi). ${hidden} grup khusus-admin disembunyikan.`, 'success');
        }

        function addSelectedBlastGroups() {
            const picked = [...document.getElementById('blastGroupSel').selectedOptions]
                .map(opt => blastAvailableGroups.find(g => g.target === opt.value))
                .filter(Boolean);
            if (!picked.length) { showToast('Pilih grup dulu dari daftar', 'error'); return; }
            selectedBlastGroups = normalizeBlastGroups(selectedBlastGroups.concat(picked));
            renderSelectedBlastGroups();
            showToast(`${picked.length} grup ditambahkan`, 'success');
        }

        function saveCurrentBlastTemplate() {
            const platform = document.getElementById('blastPlatform').value;
            const message = document.getElementById('blastMessage').value.trim();
            const media_path = document.getElementById('blastMediaPath').value.trim();
            if (!message && !media_path) { showToast('Isi pesan atau media dulu', 'error'); return; }
            const name = prompt('Nama template blast:');
            if (!name || !name.trim()) return;
            api('/api/chatgrow/blast_templates', 'POST', { name: name.trim(), platform, message, media_path }).then(d => {
                if (!d.success) { showToast(d.error || 'Gagal menyimpan template', 'error'); return; }
                refreshBlastTemplates();
                showToast('Template blast disimpan', 'success');
            }).catch(() => showToast('Gagal terhubung ke server', 'error'));
        }

        function useSelectedBlastTemplate() {
            const name = document.getElementById('blastTemplateSel').value;
            const item = blastTemplatesCache.find(x => x.name === name);
            if (!item) { showToast('Pilih template blast dulu', 'error'); return; }
            const msg = document.getElementById('blastMessage');
            msg.value = item.message || '';
            document.getElementById('blastMediaPath').value = item.media_path || '';
            msg.focus();
            msg.scrollIntoView({ behavior: 'smooth', block: 'center' });
            showToast(`Template "${item.name}" dimasukkan ke pesan blast`, 'success');
        }

        function deleteSelectedBlastTemplate() {
            const platform = document.getElementById('blastPlatform').value;
            const name = document.getElementById('blastTemplateSel').value;
            if (!name) { showToast('Pilih template blast dulu', 'error'); return; }
            api('/api/chatgrow/blast_templates', 'DELETE', { name, platform }).then(() => {
                refreshBlastTemplates();
                showToast('Template blast dihapus', 'info');
            }).catch(() => showToast('Gagal terhubung ke server', 'error'));
        }

        function saveCurrentGroupList() {
            const platform = document.getElementById('blastPlatform').value;
            const session = document.getElementById('blastSessionSel').value;
            // Ikutkan juga grup yang sedang DISOROT di daftar (walau belum klik "Pakai Grup")
            const highlighted = [...document.getElementById('blastGroupSel').selectedOptions]
                .map(opt => blastAvailableGroups.find(g => g.target === opt.value)).filter(Boolean);
            const groups = normalizeBlastGroups(selectedBlastGroups.concat(highlighted));
            if (!groups.length) {
                showToast('Pilih grup dulu: sorot grup di daftar lalu klik "Pakai Grup", baru "Simpan List".', 'error');
                return;
            }
            const name = prompt(`Beri nama list grup ini (bebas), berisi ${groups.length} grup:`);
            if (!name || !name.trim()) return;
            api('/api/chatgrow/group_lists', 'POST', { name: name.trim(), platform, session, groups }).then(d => {
                if (!d.success) { showToast(d.error || 'Gagal menyimpan list grup', 'error'); return; }
                selectedBlastGroups = groups;
                renderSelectedBlastGroups();
                refreshBlastGroupLists();
                showToast(`List grup "${name.trim()}" disimpan (${groups.length} grup)`, 'success');
            }).catch(() => showToast('Gagal terhubung ke server', 'error'));
        }

        function useSelectedGroupList() {
            const name = document.getElementById('blastGroupListSel').value;
            const item = blastGroupListsCache.find(x => x.name === name);
            if (!item) { showToast('Pilih list grup dulu', 'error'); return; }
            selectedBlastGroups = normalizeBlastGroups(item.groups || []);
            renderSelectedBlastGroups();
        }

        function deleteSelectedGroupList() {
            const platform = document.getElementById('blastPlatform').value;
            const name = document.getElementById('blastGroupListSel').value;
            if (!name) { showToast('Pilih list grup dulu', 'error'); return; }
            api('/api/chatgrow/group_lists', 'DELETE', { name, platform }).then(() => {
                refreshBlastGroupLists();
                showToast('List grup dihapus', 'info');
            }).catch(() => showToast('Gagal terhubung ke server', 'error'));
        }

        function downloadBlastCsvTemplate() {
            const platform = document.getElementById('blastPlatform').value;
            const a = document.createElement('a');
            a.href = `/api/chatgrow/csv_template?platform=${encodeURIComponent(platform)}`;
            a.download = `template_blast_${platform}.csv`;
            document.body.appendChild(a);
            a.click();
            a.remove();
        }

        function pollJobGeneric(jobId, opts) {
            // opts: {bar, pct, lbl, msg, onDone}
            const tick = () => {
                fetch(`/api/youtube/job/${jobId}`).then(r => r.json()).then(s => {
                    const pct = s.pct || 0;
                    document.getElementById(opts.pct).textContent = pct + '%';
                    document.getElementById(opts.bar).style.width = pct + '%';
                    document.getElementById(opts.lbl).textContent = s.message || 'Memproses...';
                    document.getElementById(opts.msg).textContent = s.error || '';
                    if (s.done) {
                        opts.onDone(s);
                        return;
                    }
                    opts._t = setTimeout(tick, 1500);
                }).catch(() => {});
            };
            tick();
        }

        function initChatbotView() {
            if (!chatbotInitialized) {
                chatbotInitialized = true;

                // Sub-tab WhatsApp Chat / Telegram Chat / Scrape & Blast
                document.querySelectorAll('#chatSubNav .sub-tab-btn:not(.btn-settings-icon)').forEach(sub => {
                    sub.onclick = () => {
                        document.querySelectorAll('#chatSubNav .sub-tab-btn').forEach(b => b.classList.remove('active'));
                        sub.classList.add('active');
                        activeChatSub = sub.dataset.sub;
                        document.getElementById('sub-wa').classList.toggle('view-hidden', activeChatSub !== 'wa');
                        document.getElementById('sub-tg').classList.toggle('view-hidden', activeChatSub !== 'tg');
                        document.getElementById('sub-blast').classList.toggle('view-hidden', activeChatSub !== 'blast');
                        startChatSubPolling();
                    };
                });

                //  Telegram chat client 
                document.getElementById('tgcAddSession').onclick = () => {
                    tgcStartLogin((name) => { tgcActiveSession = name; refreshTgcAll(); });
                };
                document.getElementById('tgcReloadChats').onclick = () => {
                    if (!tgcActiveSession) { showToast('Pilih akun Telegram dulu', 'error'); return; }
                    loadTgcChats();
                };
                document.getElementById('tgcSendBtn').onclick = sendTgcMessage;
                document.getElementById('tgcComposerInput').addEventListener('keydown', (e) => {
                    if (e.key === 'Enter') sendTgcMessage();
                });
                document.querySelectorAll('#tgcChatFilter .chat-filter-btn').forEach(b => {
                    b.onclick = () => {
                        document.querySelectorAll('#tgcChatFilter .chat-filter-btn').forEach(x => x.classList.remove('active'));
                        b.classList.add('active');
                        tgcChatFilter = b.dataset.filter;
                        renderTgcChats();
                    };
                });
                document.getElementById('tgcChatSearch').oninput = (e) => { tgcSearchTerm = e.target.value.trim().toLowerCase(); renderTgcChats(); };
                document.getElementById('tgcSelectAll').onchange = (e) => {
                    tgcSelected.clear();
                    if (e.target.checked) {
                        let chats = tgcChatCache.filter(c => tgcChatFilter === 'all' || c.kind === tgcChatFilter);
                        if (tgcSearchTerm) chats = chats.filter(c => (c.name || c.username || '').toLowerCase().includes(tgcSearchTerm));
                        chats.forEach(c => tgcSelected.add(c.id));
                    }
                    renderTgcChats();
                };
                document.getElementById('tgcBulkDelete').onclick = bulkDeleteTgc;
                buildEmojiPop('tgcEmojiPop', 'tgcComposerInput');
                document.getElementById('tgcEmojiBtn').onclick = (e) => { e.stopPropagation(); document.getElementById('tgcEmojiPop').classList.toggle('view-hidden'); };
                document.getElementById('tgcAttachBtn').onclick = async () => {
                    if (!tgcActiveChat) { showToast('Pilih chat dulu', 'error'); return; }
                    const p = await pickMediaFile();
                    if (p) { tgcAttachPath = p; document.getElementById('tgcAttachName').textContent = p.split(/[\\/]/).pop(); document.getElementById('tgcAttachBar').classList.remove('view-hidden'); }
                };
                document.getElementById('tgcAttachClear').onclick = clearTgcAttach;

                // -- WA chat client --
                document.getElementById('waAddSession').onclick = () => {
                    const name = prompt('Nama akun WA (bebas, mis. "toko1"):');
                    if (!name || !name.trim()) return;
                    api('/api/wa/sessions', 'POST', { name: name.trim() }).then(d => {
                        if (d.success) {
                            waActiveSession = d.session.name;
                            showToast('Menyiapkan QR login...', 'info');
                            startWaQrPolling(d.session.name);
                            refreshWaAll();
                        } else showToast(d.error || 'Gagal membuat sesi', 'error');
                    }).catch(() => showToast('Gagal terhubung ke server', 'error'));
                };

                document.getElementById('waNewChat').onclick = () => {
                    if (!waActiveSession) { showToast('Pilih sesi WA dulu', 'error'); return; }
                    const phone = prompt('Nomor WhatsApp tujuan (mis. 6281234567890):');
                    if (!phone || !phone.trim()) return;
                    api('/api/wa/newchat', 'POST', { session: waActiveSession, phone: phone.trim() }).then(d => {
                        if (d.success) {
                            loadWaChats();
                            selectWaChat({ jid: d.jid, name: phone.trim() });
                            showToast('Chat baru siap', 'success');
                        } else showToast(d.error || 'Gagal membuat chat', 'error');
                    }).catch(() => showToast('Gagal terhubung ke server', 'error'));
                };

                document.getElementById('waSyncBtn').onclick = () => {
                    if (!waActiveSession) { showToast('Pilih sesi WA dulu', 'error'); return; }
                    if (!confirm('Sinkron riwayat chat?\nSesi WA akan tersambung-ulang sebentar (tanpa scan QR) supaya WhatsApp mengirim ulang riwayat personal & grup ~30 hari terakhir. Tunggu ~30 detik.')) return;
                    api(`/api/wa/resync/${encodeURIComponent(waActiveSession)}`, 'POST').then(d => {
                        if (d.success) showToast('Menyinkron riwayat... sesi tersambung ulang, tunggu ~30 detik lalu chat akan terisi.', 'info');
                        else showToast(d.error || 'Gagal sinkron', 'error');
                    }).catch(() => showToast('Gagal terhubung ke server', 'error'));
                };
                document.getElementById('waSendBtn').onclick = sendWaMessage;
                document.getElementById('waComposerInput').addEventListener('keydown', (e) => {
                    if (e.key === 'Enter') sendWaMessage();
                });
                document.querySelectorAll('#waChatFilter .chat-filter-btn').forEach(b => {
                    b.onclick = () => {
                        document.querySelectorAll('#waChatFilter .chat-filter-btn').forEach(x => x.classList.remove('active'));
                        b.classList.add('active');
                        waChatFilter = b.dataset.filter;
                        renderWaChats();
                    };
                });
                document.getElementById('waChatSearch').oninput = (e) => { waSearchTerm = e.target.value.trim().toLowerCase(); renderWaChats(); };
                document.getElementById('waSelectAll').onchange = (e) => {
                    waSelected.clear();
                    if (e.target.checked) {
                        let chats = waChatCache.filter(c => waChatFilter === 'all' || chatKind(c) === waChatFilter);
                        if (waSearchTerm) chats = chats.filter(c => [c.name || '', c.jid || '', c.last_text || ''].join(' ').toLowerCase().includes(waSearchTerm));
                        chats.forEach(c => waSelected.add(c.jid));
                    }
                    renderWaChats();
                };
                document.getElementById('waBulkDelete').onclick = bulkDeleteWa;
                buildEmojiPop('waEmojiPop', 'waComposerInput');
                document.getElementById('waEmojiBtn').onclick = (e) => { e.stopPropagation(); document.getElementById('waEmojiPop').classList.toggle('view-hidden'); };
                document.getElementById('waAttachBtn').onclick = async () => {
                    if (!waActiveChat) { showToast('Pilih chat dulu', 'error'); return; }
                    const p = await pickMediaFile();
                    if (p) { waAttachPath = p; document.getElementById('waAttachName').textContent = p.split(/[\\/]/).pop(); document.getElementById('waAttachBar').classList.remove('view-hidden'); }
                };
                document.getElementById('waAttachClear').onclick = clearWaAttach;
                document.getElementById('waVoiceBtn').onclick = () => showToast('Voice note segera hadir', 'info');
                // Tutup emoji popover saat klik di luar
                document.addEventListener('click', (e) => {
                    ['waEmojiPop', 'tgcEmojiPop'].forEach(id => {
                        const pop = document.getElementById(id);
                        const btnId = id === 'waEmojiPop' ? 'waEmojiBtn' : 'tgcEmojiBtn';
                        if (pop && !pop.classList.contains('view-hidden') && !pop.contains(e.target) && e.target.closest('#' + btnId) === null) {
                            pop.classList.add('view-hidden');
                        }
                    });
                });

                // -- Telegram: login sesi baru (reuse alur klien Telegram) --
                document.getElementById('tgAddSession').onclick = () => {
                    tgcStartLogin(() => refreshBlastPanel());
                };

                // Toggle mode scrape (Member Grup / Histori Chat)
                document.querySelectorAll('#tgScrapeMode .scrape-mode-btn').forEach(btn => {
                    btn.onclick = () => {
                        document.querySelectorAll('#tgScrapeMode .scrape-mode-btn').forEach(b => b.classList.remove('active'));
                        btn.classList.add('active');
                        tgScrapeMode = btn.dataset.mode;
                        document.getElementById('tgScrapeHint').textContent = tgScrapeMode === 'history'
                            ? 'Kumpulkan pengirim unik dari histori chat grup/channel (untuk grup yang tidak menampilkan daftar member).'
                            : tgScrapeMode === 'channel'
                                ? 'Scrape data target channel Telegram yang kamu ikuti atau bisa diakses dari akun ini.'
                                : 'Ambil daftar member dari grup/channel yang kamu ikuti.';
                        document.getElementById('tgScrapeBtn').innerHTML = tgScrapeMode === 'history'
                            ? '<i class="fa-solid fa-clock-rotate-left"></i> Scrape Histori Telegram'
                            : tgScrapeMode === 'channel'
                                ? '<i class="fa-solid fa-bullhorn"></i> Scrape Channel Telegram'
                                : '<i class="fa-solid fa-users"></i> Scrape Member Telegram';
                    };
                });

                document.querySelectorAll('#waScrapeMode .scrape-mode-btn').forEach(btn => {
                    btn.onclick = () => {
                        document.querySelectorAll('#waScrapeMode .scrape-mode-btn').forEach(b => b.classList.remove('active'));
                        btn.classList.add('active');
                        waScrapeMode = btn.dataset.mode;
                        const isHist = waScrapeMode === 'history';
                        document.getElementById('waGroupLabel').textContent = isHist ? 'Chat (kosongkan = semua chat)' : 'Grup WhatsApp';
                        document.getElementById('waScrapeHint').textContent = isHist
                            ? 'Kumpulkan pengirim unik dari histori chat yang sudah tersimpan (pesan yang masuk selama sesi aktif).'
                            : 'Ambil daftar member dari grup WhatsApp yang kamu ikuti.';
                        document.getElementById('waScrapeBtn').innerHTML = isHist
                            ? '<i class="fa-solid fa-clock-rotate-left"></i> Scrape Histori Chat WA'
                            : '<i class="fa-solid fa-users"></i> Scrape Member Grup WA';
                        loadWaScrapeOptions();
                    };
                });

                document.getElementById('tgLogout').onclick = () => {
                    const name = document.getElementById('tgSessionSel').value;
                    if (!name) { showToast('Pilih sesi Telegram dulu', 'error'); return; }
                    if (!confirm(`Logout sesi Telegram "${name}"?`)) return;
                    api('/api/tg/logout', 'POST', { name }).then(() => { showToast('Sesi Telegram logout', 'info'); refreshBlastPanel(); });
                };

                document.getElementById('tgLoadDialogs').onclick = () => {
                    const name = document.getElementById('tgSessionSel').value;
                    if (!name) { showToast('Pilih sesi Telegram dulu', 'error'); return; }
                    showToast('Memuat daftar grup/channel...', 'info');
                    fetch(`/api/tg/dialogs/${encodeURIComponent(name)}`).then(r => r.json()).then(d => {
                        if (d.error) { showToast(d.error, 'error'); return; }
                        const dl = document.getElementById('tgDialogList');
                        dl.innerHTML = '';
                        (d.dialogs || []).forEach(g => {
                            const label = (g.username ? '@' + g.username : g.id) + ' - ' + g.title + (g.participants ? ` (${g.participants})` : '');
                            const opt = document.createElement('option');
                            opt.value = g.username ? '@' + g.username : g.id;
                            opt.label = label;
                            dl.appendChild(opt);
                        });
                        showToast(`${(d.dialogs || []).length} grup/channel dimuat - ketik di kolom target`, 'success');
                    }).catch(() => showToast('Gagal memuat', 'error'));
                };

                document.getElementById('tgScrapeBtn').onclick = () => {
                    const name = document.getElementById('tgSessionSel').value;
                    const target = document.getElementById('tgTarget').value.trim();
                    const save_name = document.getElementById('tgScrapeSaveName').value.trim();
                    if (!name) { showToast('Pilih/ login sesi Telegram dulu', 'error'); return; }
                    if (!target) { showToast('Isi target grup/channel', 'error'); return; }
                    startScrapeJob('/api/tg/scrape', { name, target, mode: tgScrapeMode, save_name });
                };

                // -- WhatsApp scrape --
                document.getElementById('waLoadGroups').onclick = loadWaScrapeOptions;

                document.getElementById('waScrapeBtn').onclick = () => {
                    const session = document.getElementById('waBlastSession').value;
                    const group_jid = document.getElementById('waGroupSel').value;
                    const save_name = document.getElementById('waScrapeSaveName').value.trim();
                    if (!session) { showToast('Pilih sesi WA dulu', 'error'); return; }
                    if (waScrapeMode === 'members' && !group_jid) { showToast('Pilih grup dulu (klik muat grup)', 'error'); return; }
                    startScrapeJob('/api/wa/scrape', { session, group_jid, mode: waScrapeMode, save_name });
                };

                document.getElementById('btnOpenScrapeFolder').onclick = () => {
                    api('/api/chatgrow/open_folder', 'POST').then(d => {
                        if (!d.success) showToast(d.error || 'Gagal membuka folder', 'error');
                    }).catch(() => showToast('Gagal terhubung ke server', 'error'));
                };

                // -- Blast --
                document.getElementById('blastPlatform').onchange = () => { updateBlastSessionOptions(); updateBlastTargetInfo(); };
                document.getElementById('blastStartBtn').onclick = startBlast;
                document.getElementById('btnSaveBlastTemplate2').onclick = saveCurrentBlastTemplate;
                document.getElementById('btnDownloadBlastCsvTemplate').onclick = downloadBlastCsvTemplate;
                document.getElementById('btnLoadBlastTemplate').onclick = useSelectedBlastTemplate;
                document.getElementById('btnDeleteBlastTemplate').onclick = deleteSelectedBlastTemplate;
                document.getElementById('btnLoadBlastGroups').onclick = loadBlastGroups;
                document.getElementById('btnPublicBlastGroups').onclick = filterPublicBlastGroups;
                document.getElementById('btnAddBlastGroups').onclick = addSelectedBlastGroups;
                document.getElementById('btnSaveGroupList').onclick = saveCurrentGroupList;
                document.getElementById('btnUseGroupList').onclick = useSelectedGroupList;
                document.getElementById('btnDeleteGroupList').onclick = deleteSelectedGroupList;
                document.getElementById('blastManual').addEventListener('input', updateBlastTargetInfo);
                document.getElementById('blastCsvPath').addEventListener('input', updateBlastTargetInfo);
                document.getElementById('blastPersonalStart').addEventListener('input', updateBlastTargetInfo);
                document.getElementById('blastPersonalEnd').addEventListener('input', updateBlastTargetInfo);
                renderSelectedBlastGroups();

                const cgModal = document.getElementById('chatgrowSettingsModal');
                document.getElementById('btnChatgrowSettings').onclick = () => {
                    api('/api/chatgrow/settings', 'GET').then(cfg => {
                        document.getElementById('cgApiId').value = cfg.tg_api_id || '';
                        document.getElementById('cgApiHash').value = cfg.tg_api_hash || '';
                        document.getElementById('cgBlastDelay').value = cfg.blast_delay || 8;
                        cgModal.classList.add('open');
                    });
                };
                const closeCg = () => cgModal.classList.remove('open');
                document.getElementById('btnCancelChatgrowSettings').onclick = closeCg;
                document.getElementById('btnCloseChatgrowSettings').onclick = closeCg;
                cgModal.addEventListener('click', (e) => { if (e.target === cgModal) closeCg(); });
                document.getElementById('btnOpenMyTelegram').onclick = () => {
                    api('/api/chatgrow/open_url', 'POST', { url: 'https://my.telegram.org/apps' }).then(d => {
                        if (d.success) showToast('Membuka my.telegram.org di browser...', 'info');
                        else showToast(d.error || 'Buka manual: my.telegram.org', 'error');
                    }).catch(() => showToast('Buka manual: my.telegram.org', 'error'));
                };
                document.getElementById('btnSaveChatgrowSettings').onclick = () => {
                    api('/api/chatgrow/settings', 'POST', {
                        tg_api_id: document.getElementById('cgApiId').value.trim(),
                        tg_api_hash: document.getElementById('cgApiHash').value.trim(),
                        blast_delay: document.getElementById('cgBlastDelay').value,
                    }).then(res => {
                        if (res.success) { showToast('Pengaturan disimpan', 'success'); closeCg(); }
                        else showToast(res.error || 'Gagal menyimpan', 'error');
                    }).catch(() => showToast('Gagal terhubung ke server', 'error'));
                };
            }
            startChatSubPolling();
        }

        function loadWaScrapeOptions() {
            const name = document.getElementById('waBlastSession').value;
            if (!name) { showToast('Pilih sesi WA dulu', 'error'); return; }
            if (waScrapeMode === 'history') {
                // Histori: daftar chat tersimpan + opsi "semua chat"
                fetch(`/api/wa/chats/${encodeURIComponent(name)}`).then(r => r.json()).then(d => {
                    const chats = d.chats || [];
                    const items = [{ v: '', t: '(Semua chat tersimpan)' }]
                        .concat(chats.map(c => ({ v: c.jid, t: (c.is_group ? '[grup] ' : '') + c.name })));
                    fillSelect('waGroupSel', items, '(Semua chat tersimpan)');
                    showToast(`${chats.length} chat tersimpan`, 'success');
                }).catch(() => showToast('Gagal memuat chat', 'error'));
            } else {
                showToast('Memuat daftar grup...', 'info');
                fetch(`/api/wa/groups/${encodeURIComponent(name)}`).then(r => r.json()).then(d => {
                    if (d.error) { showToast(d.error, 'error'); return; }
                    fillSelect('waGroupSel', (d.groups || []).map(g => ({ v: g.jid, t: `${g.name} (${g.participants})` })), 'Tidak ada grup');
                    showToast(`${(d.groups || []).length} grup dimuat`, 'success');
                }).catch(() => showToast('Gagal memuat grup', 'error'));
            }
        }

        function startScrapeJob(url, payload) {
            const panel = document.getElementById('scrapeProgress');
            revealProgressPanel(panel);
            document.getElementById('scrapeProgLbl').textContent = 'Memulai scrape...';
            document.getElementById('scrapeProgPctTxt').textContent = '0%';
            document.getElementById('scrapeProgBar').style.width = '0%';
            document.getElementById('scrapeProgMsg2').textContent = '';
            const platform = url === '/api/tg/scrape' ? 'telegram' : 'whatsapp';
            const session = platform === 'telegram' ? payload.name : payload.session;
            api(url, 'POST', payload).then(d => {
                if (!d.success) { showToast(d.error || 'Gagal memulai scrape', 'error'); return; }
                if (scrapePollTimer) clearTimeout(scrapePollTimer);
                pollJobGeneric(d.job_id, {
                    bar: 'scrapeProgBar', pct: 'scrapeProgPctTxt', lbl: 'scrapeProgLbl', msg: 'scrapeProgMsg2',
                    onDone: (s) => {
                        if (s.success) {
                            showToast(`Scrape selesai: ${s.count || 0} member -> ${s.file || ''}`, 'success');
                            refreshScrapeFiles();
                            if (s.file) sendScrapeResultToBlast(s.file, platform, session);
                        } else showToast(s.error || 'Scrape gagal', 'error');
                    }
                });
            }).catch(() => showToast('Gagal terhubung ke server', 'error'));
        }

        // Otomatis jadikan hasil scrape yang baru selesai sebagai target Blast
        // (platform + sesi pengirim ikut disamakan) - user tinggal isi pesan
        // & klik Blast, tanpa perlu cari/pilih file CSV-nya manual lagi.
        function sendScrapeResultToBlast(file, platform, session) {
            selectedScrapeFile = file;
            refreshScrapeFiles();
            const platformSel = document.getElementById('blastPlatform');
            if (platformSel.value !== platform) platformSel.value = platform;
            updateBlastSessionOptions();
            const sessionSel = document.getElementById('blastSessionSel');
            if (session && [...sessionSel.options].some(o => o.value === session)) {
                sessionSel.value = session;
            }
            updateBlastTargetInfo();
            revealProgressPanel(document.getElementById('blastTargetInfo'));
            showToast('Hasil scrape siap diblast - tinggal isi pesan & klik Blast', 'success');
        }

        function startBlast() {
            const platform = document.getElementById('blastPlatform').value;
            const session = document.getElementById('blastSessionSel').value;
            const message = document.getElementById('blastMessage').value.trim();
            const media_path = document.getElementById('blastMediaPath').value.trim();
            const csv_path = document.getElementById('blastCsvPath').value.trim();
            const manual = document.getElementById('blastManual').value.split('\n').map(s => s.trim()).filter(Boolean);
            const personal_start = document.getElementById('blastPersonalStart').value.trim();
            const personal_end = document.getElementById('blastPersonalEnd').value.trim();
            if (!session) { showToast('Pilih sesi pengirim dulu', 'error'); return; }
            if (!message && !media_path && !csv_path) { showToast('Isi pesan blast, isi kolom message di CSV, atau pilih media dulu', 'error'); return; }
            if (!selectedScrapeFile && !csv_path && !manual.length && !selectedBlastGroups.length) {
                showToast('Pilih file scrape, CSV custom, target manual, atau grup dulu', 'error'); return;
            }
            const payload = { message, media_path };
            if (platform === 'telegram') payload.name = session; else payload.session = session;
            if (selectedScrapeFile) payload.csv_file = selectedScrapeFile;
            if (csv_path) payload.csv_path = csv_path;
            if (manual.length) payload.targets = manual;
            if (selectedBlastGroups.length) payload.group_targets = selectedBlastGroups;
            if (personal_start) payload.personal_start = personal_start;
            if (personal_end) payload.personal_end = personal_end;

            const total = selectedScrapeFile || csv_path ? '?' : (manual.length + selectedBlastGroups.length);
            if (!confirm(`Kirim blast ke ${total} target via ${platform.toUpperCase()} (sesi ${session})?
Gunakan jeda aman untuk hindari ban.`)) return;

            const url = platform === 'telegram' ? '/api/tg/blast' : '/api/wa/blast';
            const panel = document.getElementById('blastProgress');
            revealProgressPanel(panel);
            document.getElementById('blastProgLbl').textContent = 'Memulai blast...';
            document.getElementById('blastProgPctTxt').textContent = '0%';
            document.getElementById('blastProgBar').style.width = '0%';
            document.getElementById('blastProgMsg').textContent = '';
            const btn = document.getElementById('blastStartBtn');
            btn.disabled = true;
            api(url, 'POST', payload).then(d => {
                if (!d.success) { btn.disabled = false; showToast(d.error || 'Gagal memulai blast', 'error'); return; }
                if (blastPollTimer) clearTimeout(blastPollTimer);
                pollJobGeneric(d.job_id, {
                    bar: 'blastProgBar', pct: 'blastProgPctTxt', lbl: 'blastProgLbl', msg: 'blastProgMsg',
                    onDone: (s) => {
                        btn.disabled = false;
                        if (s.success) showToast(`Blast selesai: ${s.sent || 0}/${s.total || 0} terkirim`, 'success');
                        else showToast(s.error || 'Blast gagal', 'error');
                    }
                });
            }).catch(() => { btn.disabled = false; showToast('Gagal terhubung ke server', 'error'); });
        }

        // ---------- Upload YouTube ----------
        const upAccount = document.getElementById('upAccount');
        const upChannel = document.getElementById('upChannel');
        const fbAccountAliasField = document.getElementById('fbAccountAliasField');
        const fbAccountAlias = document.getElementById('fbAccountAlias');
        const btnSaveFbAccountAlias = document.getElementById('btnSaveFbAccountAlias');
        const btnStartUpload = document.getElementById('btnStartUpload');
        const btnStopUpload = document.getElementById('btnStopUpload');
        const btnStopMultiUpload = document.getElementById('btnStopMultiUpload');
        let uploadPolling = null;
        let activeUploadJobId = '';
        let uploadMode = 'batch';

        // Mode toggle (batch / multi)
        document.querySelectorAll('.mode-btn').forEach(b => {
            b.onclick = () => {
                uploadMode = b.dataset.mode;
                document.querySelectorAll('.mode-btn').forEach(x => x.classList.toggle('active', x === b));
                document.getElementById('mode-batch').classList.toggle('view-hidden', uploadMode !== 'batch');
                document.getElementById('mode-multi').classList.toggle('view-hidden', uploadMode !== 'multi');
                if (uploadMode === 'multi') renderMultiAccounts();
            };
        });

        // Channel dropdown helpers
        function channelValue(c) {
            return c.handle || c.id || c.name || '';
        }

        function channelLabel(c) {
            let name = (c.name && !c.name.startsWith('UC')) ? c.name : (c.handle || c.id);
            if (c.handle) name += ` (${c.handle})`;
            const subs = (c.subscribers || '').trim();
            if (subs) {
                const zero = /^(no subscribers|0(\s|$))/i.test(subs);
                name += zero ? ' | 0 subscriber' : ` | ${subs} subscribers`;
            }
            return name;
        }

        function fillChannelDropdown(channels, active) {
            upChannel.innerHTML = '';
            upChannel.add(new Option('Channel default (aktif)', ''));
            (channels || []).forEach(c => {
                const opt = new Option(channelLabel(c), channelValue(c));
                if (c.active) opt.selected = true;
                upChannel.add(opt);
            });
            if (active) {
                for (const o of upChannel.options) {
                    if (o.value && o.value.toLowerCase() === active.toLowerCase()) { o.selected = true; break; }
                }
            }
        }

        function loadCachedChannels(profile) {
            const p = allProfiles.find(x => x.name === profile);
            fillChannelDropdown(p ? p.channels : []);
        }

        function renderFbAccountAliasChips(profile) {
            const box = document.getElementById('fbAccountAliasChips');
            if (!box) return;
            box.innerHTML = '';
            getPostingLabels(profile).forEach(label => {
                const chip = document.createElement('span');
                chip.className = 'label-chip';
                chip.innerHTML = `<i class="fa-solid fa-bookmark"></i> ${escapeHtml(label)} <i class="fa-solid fa-xmark" title="Hapus label"></i>`;
                chip.querySelector('.fa-xmark').onclick = () => removePostingLabelFromUploader(label);
                box.appendChild(chip);
            });
        }

        function syncPostingLabel() {
            const profile = getProfileByName(upAccount.value);
            fbAccountAlias.value = '';
            renderFbAccountAliasChips(profile);
        }

        function applySavedPostingLabel(profileName, data) {
            const profile = getProfileByName(profileName);
            if (profile) {
                profile.posting_labels = data.posting_labels || [];
                profile.posting_label = data.posting_label || '';
                profile.facebook_label = data.facebook_label || '';
            }
            populateUploadAccounts(profileName);
            syncPostingLabel();
            if (uploadMode === 'multi') renderMultiAccounts();
            applyFilters();
        }

        function savePostingLabelFromUploader() {
            const profileName = upAccount.value;
            if (!profileName) { showToast('Pilih akun dulu', 'error'); return; }
            const posting_label = fbAccountAlias.value.trim();
            if (!posting_label) { showToast('Isi label dulu', 'error'); return; }
            btnSaveFbAccountAlias.disabled = true;
            api(`/api/profiles/${encodeURIComponent(profileName)}/posting_labels`, 'POST', { posting_label }).then(data => {
                btnSaveFbAccountAlias.disabled = false;
                if (!data.success) { showToast(data.error || 'Gagal menambah label akun', 'error'); return; }
                applySavedPostingLabel(profileName, data);
                showToast('Label akun ditambahkan', 'success');
            }).catch(() => {
                btnSaveFbAccountAlias.disabled = false;
                showToast('Gagal terhubung ke server', 'error');
            });
        }

        function removePostingLabelFromUploader(label) {
            const profileName = upAccount.value;
            if (!profileName) return;
            api(`/api/profiles/${encodeURIComponent(profileName)}/posting_labels`, 'DELETE', { posting_label: label }).then(data => {
                if (!data.success) { showToast(data.error || 'Gagal menghapus label akun', 'error'); return; }
                applySavedPostingLabel(profileName, data);
                showToast(`Label "${label}" dihapus`, 'info');
            }).catch(() => showToast('Gagal terhubung ke server', 'error'));
        }

        btnSaveFbAccountAlias.addEventListener('click', savePostingLabelFromUploader);
        fbAccountAlias.addEventListener('keydown', e => {
            if (e.key === 'Enter') {
                e.preventDefault();
                savePostingLabelFromUploader();
            }
        });

        upAccount.addEventListener('change', () => {
            loadCachedChannels(upAccount.value);
            syncPostingLabel();
            const fbTgt = uploadPlatform === 'facebook' ? document.getElementById('fbTarget').value : '';
            if (fbTgt === 'page') {
                document.getElementById('fbPageManual').value = '';
                loadCachedPages(upAccount.value);
            }
            if (fbTgt === 'group') {
                loadCachedGroups(upAccount.value);
            }
        });

        document.getElementById('btnDetectChannel').onclick = () => {
            const profile = upAccount.value;
            if (!profile) { showToast('Pilih akun dulu', 'error'); return; }
            showToast('Membuka profil untuk deteksi channel... (bisa 20-40 detik)', 'info');
            api('/api/youtube/channels', 'POST', { profile }).then(data => {
                if (!data.success) { showToast(data.error || 'Gagal', 'error'); return; }
                const poll = setInterval(() => {
                    fetch(`/api/youtube/job/${data.job_id}`).then(r => r.json()).then(s => {
                        if (!s.done) return;
                        clearInterval(poll);
                        if (s.success) {
                            const chans = s.channels || [];
                            fillChannelDropdown(chans, s.active);
                            // simpan ke cache
                            api('/api/youtube/channels/save', 'POST', { profile, channels: chans });
                            const p = allProfiles.find(x => x.name === profile);
                            if (p) p.channels = chans;
                            showToast(`${chans.length} channel ditemukan`, 'success');
                        } else {
                            showToast('Deteksi gagal: ' + (s.error || ''), 'error');
                        }
                    }).catch(() => {});
                }, 1500);
            }).catch(() => showToast('Gagal terhubung ke server', 'error'));
        };

        // Unduh template CSV (native save dialog di desktop, download di browser)
        document.getElementById('btnDownloadTemplate').onclick = async () => {
            const P = uploadPlatform;
            const isTt = P === 'tiktok', isFb = P === 'facebook', isIg = P === 'instagram';
            const social = ['pinterest', 'threads', 'x'].includes(P);
            const kind = isTt ? 'tiktok' : isFb ? 'facebook' : isIg ? 'instagram' : social ? P : 'youtube';
            const upType = document.getElementById('upType').value;
            const suffix = (upType === 'affiliate' || upType === 'seller') ? 'affiliate' : 'regular';
            const href = isTt ? `/api/tiktok/csv_template?type=${upType}` : isFb ? '/api/facebook/csv_template'
                : isIg ? '/api/instagram/csv_template' : social ? `/api/social/${P}/csv_template` : '/api/youtube/csv_template';
            const filename = isTt ? `template_tiktok_${suffix}.csv` : isFb ? 'template_upload_facebook.csv'
                : isIg ? 'template_upload_instagram.csv' : social ? `template_upload_${P}.csv` : 'template_upload_youtube.csv';
            // Desktop: dialog Save-As native -> user bebas pilih folder & nama file
            if (window.pywebview && window.pywebview.api && window.pywebview.api.save_csv_template) {
                try {
                    const path = await window.pywebview.api.save_csv_template(kind, upType);
                    if (path) {
                        showToast('Template disimpan: ' + path, 'success');
                        document.getElementById('upCsv').value = path;
                    } else {
                        showToast('Penyimpanan template dibatalkan', 'info');
                    }
                } catch (e) { showToast('Gagal menyimpan template', 'error'); }
            } else {
                const a = document.createElement('a');
                a.href = href;
                a.download = filename;
                document.body.appendChild(a);
                a.click();
                a.remove();
                showToast('Template CSV diunduh', 'success');
            }
        };

        // CSV preview
        document.getElementById('btnPreviewCsv').onclick = () => {
            const csv_path = document.getElementById('upCsv').value.trim();
            if (!csv_path) { showToast('Isi path CSV dulu', 'error'); return; }
            const P = uploadPlatform;
            const isTt = P === 'tiktok', isFb = P === 'facebook', isIg = P === 'instagram';
            const social = ['pinterest', 'threads', 'x'].includes(P);
            const isCaption = isTt || isFb || isIg || social;
            const previewUrl = isTt ? '/api/tiktok/csv_preview' : isFb ? '/api/facebook/csv_preview'
                : isIg ? '/api/instagram/csv_preview' : social ? `/api/social/${P}/csv_preview` : '/api/youtube/csv_preview';
            api(previewUrl, 'POST', { csv_path }).then(data => {
                const table = document.getElementById('csvPreview');
                if (data.error) { showToast(data.error, 'error'); table.innerHTML = ''; return; }
                const rows = data.rows || [];
                if (!rows.length) { table.innerHTML = '<tr><td>CSV kosong / tak dikenali</td></tr>'; return; }
                const titleHeader = isCaption ? 'Caption' : 'Judul';
                const isYt = P === 'youtube';
                // Ceklis sebelum upload (khusus YouTube): kolom ekstra supaya
                // thumbnail hilang/salah path, atau lupa tandai AI-content /
                // Made-for-Kids ketahuan SEBELUM upload jalan, bukan sesudah.
                const ytCols = isYt ? '<th title="Thumbnail ada di disk?">Thumb</th><th title="Altered/synthetic content disclosure">AI</th><th title="Made for Kids">Kids</th>' : '';
                // TikTok: kolom status per baris - baris invalid (path salah/
                // caption kosong/jadwal salah) ditandai jelas & di-skip
                // otomatis, BUKAN menjatuhkan/mengosongkan seluruh preview.
                const ttCols = isTt ? '<th title="Valid untuk diupload">Status</th>' : '';
                let html = `<thead><tr><th class="sel-col"><input type="checkbox" id="selAll" checked title="Pilih semua"></th><th>#</th><th>${titleHeader}</th><th>Video</th><th>Jadwal</th>${ytCols}${ttCols}</tr></thead><tbody>`;
                const flag = (ok, onLabel, offLabel) => ok
                    ? `<span class="csv-flag csv-flag-on" title="${onLabel}"><i class="fa-solid fa-check"></i></span>`
                    : `<span class="csv-flag csv-flag-off" title="${offLabel}">-</span>`;
                let invalidCount = 0;
                rows.forEach((r, i) => {
                    const sched = (r.date && r.time) ? `${r.date} ${r.time}` : 'Publish langsung';
                    const title = isCaption ? (r.caption || '') : (r.title || '');
                    let ytCells = '';
                    if (isYt) {
                        const hasThumb = !!(r.thumbnail_path || '').trim();
                        const thumbFlag = !hasThumb ? '<span class="csv-flag" title="Tanpa thumbnail (opsional)">-</span>'
                            : flag(r.thumbnail_ok, 'Thumbnail ditemukan di disk', 'File thumbnail TIDAK ditemukan - cek path!');
                        ytCells = `<td>${thumbFlag}</td><td>${flag(r.ai_content, 'AI content: YA', 'AI content: tidak')}</td>`
                            + `<td>${flag(r.made_for_kids, 'Made for Kids: YA', 'Made for Kids: tidak (default)')}</td>`;
                    }
                    const rowError = isTt ? (r._error || '') : '';
                    if (rowError) invalidCount++;
                    const ttCells = isTt ? `<td>${rowError
                        ? `<span class="csv-status-pill csv-flag-off" title="${escapeHtml(rowError)}"><i class="fa-solid fa-triangle-exclamation"></i> invalid</span>`
                        : `<span class="csv-status-pill csv-flag-on" title="Valid"><i class="fa-solid fa-check"></i> ok</span>`}</td>` : '';
                    const trAttr = rowError ? ` class="csv-row-invalid" title="${escapeHtml(rowError)}"` : '';
                    html += `<tr${trAttr}><td class="sel-col"><input type="checkbox" class="rowSel" data-idx="${i}" ${rowError ? 'disabled' : 'checked'}></td><td>${i+1}</td><td>${escapeHtml(title)}</td><td>${escapeHtml((r.video_path||'').split(/[\\/]/).pop())}</td><td>${escapeHtml(sched)}</td>${ytCells}${ttCells}</tr>`;
                });
                html += '</tbody>';
                table.innerHTML = html;
                wireCsvSelection();
                if (invalidCount) {
                    showToast(`${rows.length} baris dimuat, ${invalidCount} INVALID (dilewati otomatis - arahkan mouse ke baris merah untuk alasannya)`, 'info');
                } else {
                showToast(`${rows.length} baris dimuat`, 'success');
                }
            }).catch(() => showToast('Gagal terhubung ke server', 'error'));
        };

        // Centang/pilih baris CSV yang akan diupload
        function updateCsvSelInfo() {
            const total = document.querySelectorAll('#csvPreview .rowSel').length;
            const sel = document.querySelectorAll('#csvPreview .rowSel:checked').length;
            const info = document.getElementById('csvSelInfo');
            info.textContent = `${sel} dari ${total} video dipilih untuk diupload`;
            info.classList.toggle('view-hidden', total === 0);
        }
        function wireCsvSelection() {
            const selAll = document.getElementById('selAll');
            const boxes = document.querySelectorAll('#csvPreview .rowSel');
            if (selAll) selAll.onchange = () => { boxes.forEach(b => b.checked = selAll.checked); updateCsvSelInfo(); };
            boxes.forEach(b => b.onchange = () => {
                if (selAll) selAll.checked = [...boxes].every(x => x.checked);
                updateCsvSelInfo();
            });
            updateCsvSelInfo();
        }
        function getSelectedCsvRows() {
            return [...document.querySelectorAll('#csvPreview .rowSel:checked')].map(c => parseInt(c.dataset.idx, 10));
        }

        const upPlatform = document.getElementById('upPlatform');

        // Cek ketersediaan engine per platform
        fetch('/api/youtube/available').then(r => r.json()).then(d => {
            ytAvailable = d.available;
            platformAvail = d.platforms || {};
            applyPlatform();
        }).catch(() => {});

        function applyPlatform() {
            uploadPlatform = upPlatform.value;
            const P = uploadPlatform;
            const isYt = P === 'youtube', isTt = P === 'tiktok', isFb = P === 'facebook',
                  isIg = P === 'instagram', isPin = P === 'pinterest',
                  isThreads = P === 'threads', isX = P === 'x';
            const captionLabel = isTt || isFb || isIg || isThreads || isX;
            const schedOk = isYt || isTt || isPin || isX;   // platform yang dukung jadwal
            const batchOk = true;                           // semua platform dukung batch
            const hints = {
                youtube: '',
                tiktok: 'TikTok memakai sesi login di profil ini - pastikan sudah login TikTok dari shortcut TikTok di kartu profil.',
                facebook: 'Facebook memakai sesi login di profil ini. Untuk Fanpage, upload diarahkan lewat Dashboard Profesional Page: pilih Fanpage, unggah video atau foto, isi caption, lalu jadwalkan jika CSV berisi tanggal+jam. Untuk awal, coba mode terlihat + 1 file.',
                instagram: 'Instagram memakai sesi login di profil ini. Upload Reels (video) atau Foto/Post biasa. Untuk pertama kali coba mode terlihat + 1 file.',
                pinterest: 'Pinterest memakai sesi login di profil ini (buka shortcut Pinterest lalu login). Media (gambar/video) wajib. Judul + Deskripsi + link tujuan. Mendukung jadwal.',
                threads: 'Threads memakai sesi login di profil ini (buka shortcut Threads lalu login). Teks + media opsional.',
                x: 'X memakai sesi login di profil ini (buka shortcut X lalu login). Teks + media opsional. Mendukung jadwal (best-effort).',
            };
            document.getElementById('channelField').classList.toggle('view-hidden', !isYt);
            document.getElementById('youtubeBar').classList.toggle('view-hidden', !isYt);
            document.getElementById('tiktokTypeBar').classList.toggle('view-hidden', !isTt);
            document.getElementById('tiktokBar').classList.toggle('view-hidden', !isTt);
            document.getElementById('facebookBar').classList.toggle('view-hidden', !isFb);
            syncPostingLabel();
            if (isFb) updateFbTarget();
            const csvHints = {
                tiktok: 'Kolom: <code>video_path; caption; hashtags; location; ai_generate_content; product_name; product_id; schedule_date; schedule_time</code>. <b>location</b>: nama kota/lokasi opsional per video (hanya mode Regular). <b>ai_generate_content</b>: isi <code>yes</code> jika konten dibuat AI (bisa di-override global dari checkbox di atas). Isi <code>product_id</code> (wajib) untuk Affiliate/Seller.',
                facebook: 'Kolom: <code>video_path; caption; schedule_date; schedule_time; publish_mode; post_delay; group_id; group_name</code>. video_path opsional untuk target Grup (bisa post teks saja). <b>Target Fanpage:</b> upload via Meta Business Suite, isi <code>publish_mode</code> (<code>schedule</code>/<code>now</code>). <b>Target Grup:</b> isi <code>group_id</code> (ID numerik/slug dari URL facebook.com/groups/ID). <b>post_delay</b>: jeda detik antar post.',
                instagram: 'Kolom: <code>video_path;caption</code>. Satu baris = satu Reels atau Foto. video_path wajib (video <i>atau</i> foto).',
                pinterest: 'Kolom: <code>video_path; title; caption; link; schedule_date; schedule_time</code>. Media wajib. Isi tanggal+jam untuk jadwal.',
                threads: 'Kolom: <code>video_path; caption</code>. video_path opsional (boleh teks saja).',
                x: 'Kolom: <code>video_path; caption; schedule_date; schedule_time</code>. video_path opsional.',
                youtube: 'Kolom: <code>VIDEO PATH; TITLE; DESCRIPTION; TAGS; THUMBNAIL; DATE; TIME; AI CONTENT</code>. Isi DATE + TIME untuk jadwal.',
            };
            document.getElementById('csvHint').innerHTML = csvHints[P] || csvHints.youtube;
            // Multi Akun tersedia untuk SEMUA platform. Hapus-setelah-posting khusus TikTok.
            document.getElementById('deleteAfterField').classList.toggle('view-hidden', !isTt);
            if (uploadMode === 'multi') renderMultiAccounts();
            const av = platformAvail[uploadPlatform];
            const box = document.getElementById('ytUnavailable');
            const hint = document.getElementById('platformHint');
            if (av && !av.available) {
                box.textContent = 'Engine belum siap: ' + av.reason;
                box.classList.remove('view-hidden');
                btnStartUpload.disabled = true;
            } else {
                box.classList.add('view-hidden');
                btnStartUpload.disabled = false;
            }
            hint.textContent = hints[uploadPlatform] || '';
            // Ganti platform -> saring ulang daftar akun berdasar grup
            const prevAccount = upAccount.value;
            populateUploadAccounts();
            if (upAccount.value !== prevAccount) {
                loadCachedChannels(upAccount.value);
                syncPostingLabel();
                if (isFb && document.getElementById('fbTarget').value === 'page') loadCachedPages(upAccount.value);
            } else {
                syncPostingLabel();
            }
        }
        upPlatform.addEventListener('change', applyPlatform);

        // Tipe upload TikTok: field produk hanya untuk affiliate/seller.
        // Location & AI Generate Content diatur dari CSV per-row / checkbox di UI.
        function updateProductFields() {
            const t = document.getElementById('upType').value;
            const hint = document.getElementById('ttTypeHint');
            hint.textContent = (t !== 'regular')
                ? 'Setiap baris CSV wajib punya kolom product_id (opsional product_name) untuk tag produk.'
                : '';
        }
        document.getElementById('upType').addEventListener('change', updateProductFields);

        // AI Generate Content TikTok: saat dicentang, flag dikirim ke backend sebagai override global.
        document.getElementById('upTtAiGenerate').addEventListener('change', function() {
            // Placeholder untuk logika tambahan jika diperlukan.
        });

        // ---------- Facebook target / fanpage ----------
        function updateFbTarget() {
            const t = document.getElementById('fbTarget').value;
            document.getElementById('fbPageField').classList.toggle('view-hidden', t !== 'page');
            document.getElementById('fbGroupField').classList.toggle('view-hidden', t !== 'group');
            // publish mode hanya untuk personal dan page, group selalu post langsung
            document.getElementById('fbPublishModeField').classList.toggle('view-hidden', t === 'group');
            if (t === 'page') loadCachedPages(upAccount.value);
            if (t === 'group') loadCachedGroups(upAccount.value);
            if (uploadMode === 'multi') renderMultiAccounts();
        }
        function updateFbPublishMode() {
            const m = document.getElementById('fbPublishMode').value;
            document.getElementById('fbPostDelayField').classList.toggle('view-hidden', m !== 'now');
        }
        document.getElementById('fbPublishMode').addEventListener('change', updateFbPublishMode);

        // ---------- Facebook Group helpers ----------
        function findGroupMeta(groups, groupId) {
            const want = String(groupId || '').trim().toLowerCase();
            if (!want) return null;
            return ((groups || []).find(g => String((g && g.id) || '').trim().toLowerCase() === want)) || null;
        }
        function fillGroupSelect(sel, groups, selected = '') {
            const want = String(selected || '').trim().toLowerCase();
            sel.innerHTML = '';
            const cleanGroups = (groups || []).filter(g => g && g.id && g.name);
            sel.add(new Option(cleanGroups.length ? 'pilih grup' : '(belum terdeteksi - ketik manual)', ''));
            for (const g of cleanGroups) {
                const opt = new Option(`${g.name} (${g.id})`, g.id);
                if (want && (g.id.toLowerCase() === want || g.name.toLowerCase() === want)) opt.selected = true;
                sel.add(opt);
            }
        }
        function loadCachedGroups(profile) {
            if (!profile) return;
            fetch('/api/facebook/groups', {method:'POST', headers:{'Content-Type':'application/json'},
                body: JSON.stringify({profile, cached_only: true})})
                .then(r => r.json()).then(d => {
                    if (d.groups) fillGroupSelect(document.getElementById('fbGroup'), d.groups);
                }).catch(() => {});
        }
        document.getElementById('fbGroup').addEventListener('change', e => {
            if (e.target.value) document.getElementById('fbGroupManual').value = '';
        });
        document.getElementById('fbGroupManual').addEventListener('input', e => {
            if (e.target.value.trim()) document.getElementById('fbGroup').value = '';
        });

        // Tombol Deteksi Grup
        document.getElementById('btnDetectGroups').addEventListener('click', () => {
            const profile = upAccount.value;
            if (!profile) { showToast('Pilih akun dulu', 'error'); return; }
            const short = profile.replace('_gmail.com','');
            showToast(`Mendeteksi grup ${short}... (bisa 20-40 detik)`, 'info');
            document.getElementById('btnDetectGroups').disabled = true;
            fetch('/api/facebook/groups', {method:'POST', headers:{'Content-Type':'application/json'},
                body: JSON.stringify({profile, cached_only: false})})
                .then(r => r.json()).then(d => {
                    if (!d.job_id) { showToast(d.error || 'Gagal memulai deteksi grup', 'error'); document.getElementById('btnDetectGroups').disabled = false; return; }
                    // Poll status
                    const poll = setInterval(() => {
                        fetch(`/api/youtube/job/${d.job_id}`).then(r => r.json()).then(s => {
                            if (!s.done) return;
                            clearInterval(poll);
                            document.getElementById('btnDetectGroups').disabled = false;
                            const groups = s.groups || [];
                            showToast(`${short}: ${groups.length} grup ditemukan`, groups.length ? 'success' : 'info');
                            if (groups.length) {
                                // Simpan ke cache
                                fetch('/api/facebook/groups/save', {method:'POST', headers:{'Content-Type':'application/json'},
                                    body: JSON.stringify({profile, groups})});
                                fillGroupSelect(document.getElementById('fbGroup'), groups);
                                // Update profil di allProfiles
                                const p = allProfiles.find(x => x.name === profile);
                                if (p) p.fb_groups = groups;
                            }
                        }).catch(() => { clearInterval(poll); document.getElementById('btnDetectGroups').disabled = false; });
                    }, 2000);
                }).catch(() => { document.getElementById('btnDetectGroups').disabled = false; });
        });
        function sanitizeDetectedPages(pages) {
            const reject = ['pesan', 'message', 'messenger', 'inbox', 'notification', 'notifikasi', 'keterangan foto tidak tersedia'];
            const seen = new Set();
            return (pages || []).filter(p => {
                const id = String((p && p.id) || '').trim();
                const name = String((p && p.name) || '').trim();
                if (!id || !name) return false;
                if (!/^\d+$/.test(id)) return false;
                const low = name.toLowerCase();
                if (reject.some(x => low === x || low.includes(x))) return false;
                if (!/[a-z]/i.test(name)) return false;
                if (seen.has(id.toLowerCase())) return false;
                seen.add(id.toLowerCase());
                return true;
            });
        }
        function isNumericFbPageId(id) {
            return /^\d+$/.test(String(id || '').trim());
        }
        function pageOptionLabel(p) {
            return `${p.name} (${p.id})`;
        }
        function findPageMeta(pages, pageId) {
            const want = String(pageId || '').trim().toLowerCase();
            if (!want) return null;
            return (sanitizeDetectedPages(pages).find(p => String((p && p.id) || '').trim().toLowerCase() === want)) || null;
        }
        function fillPageSelect(sel, pages, selected = '') {
            const cleanPages = sanitizeDetectedPages(pages);
            const want = String(selected || '').trim().toLowerCase();
            sel.innerHTML = '';
            sel.add(new Option(cleanPages.length ? 'pilih fanpage' : '(belum terdeteksi - ketik manual di bawah)', ''));
            cleanPages.forEach(p => {
                const opt = new Option(pageOptionLabel(p), p.id);
                if (want && String(p.id || '').trim().toLowerCase() === want) opt.selected = true;
                sel.add(opt);
            });
        }
        function fillPageDropdown(pages, selected = '') {
            fillPageSelect(document.getElementById('fbPage'), pages, selected);
        }
        function loadCachedPages(profile) {
            if (!profile) { fillPageDropdown([]); return; }
            const p = getProfileByName(profile);
            if (p && Array.isArray(p.fb_pages) && p.fb_pages.length) {
                fillPageDropdown(p.fb_pages);
                return;
            }
            api('/api/facebook/pages', 'POST', { profile, cached_only: true })
                .then(d => {
                    if (p) p.fb_pages = d.pages || [];
                    fillPageDropdown(d.pages || []);
                }).catch(() => fillPageDropdown([]));
        }
        document.getElementById('fbTarget').addEventListener('change', updateFbTarget);
        document.getElementById('fbPage').addEventListener('change', e => {
            if (e.target.value) document.getElementById('fbPageManual').value = '';
        });
        document.getElementById('fbPageManual').addEventListener('input', e => {
            if (e.target.value.trim()) document.getElementById('fbPage').value = '';
        });
        document.getElementById('btnDetectPages').onclick = () => {
            const profile = upAccount.value;
            if (!profile) { showToast('Pilih akun dulu', 'error'); return; }
            showToast('Membuka profil untuk deteksi Fanpage... (20-40 detik)', 'info');
            api('/api/facebook/pages', 'POST', { profile, headless: !!document.getElementById('upInvisible').checked }).then(data => {
                if (!data.success) { showToast(data.error || 'Gagal', 'error'); return; }
                const poll = setInterval(() => {
                    fetch(`/api/youtube/job/${data.job_id}`).then(r => r.json()).then(s => {
                        if (!s.done) return;
                        clearInterval(poll);
                        if (s.success) {
                            const pages = s.pages || [];
                            fillPageDropdown(pages);
                            api('/api/facebook/pages/save', 'POST', { profile, pages });
                            const p = getProfileByName(profile);
                            if (p) p.fb_pages = pages;
                            showToast(`${pages.length} Fanpage ditemukan`, pages.length ? 'success' : 'info');
                        } else showToast('Deteksi gagal: ' + (s.error || ''), 'error');
                    }).catch(() => {});
                }, 1500);
            }).catch(() => showToast('Gagal terhubung ke server', 'error'));
        };

        function populateUploadAccounts(selected) {
            const keep = selected || upAccount.value;
            const { profiles, filtered, emptyGroup } = profilesForPlatform(uploadPlatform);
            let list = profiles;
            // Profil yang diminta eksplisit (mis. dari shortcut kartu profil)
            // tetap ditampilkan walau tidak masuk grup platform ini.
            if (selected && !list.some(p => p.name === selected)) {
                const extra = allProfiles.find(x => x.name === selected);
                if (extra) list = [extra, ...list];
            }
            upAccount.innerHTML = '';
            list.forEach(p => {
                upAccount.add(new Option(getUploadAccountLabel(p, uploadPlatform), p.name));
            });
            if (keep && list.some(p => p.name === keep)) upAccount.value = keep;
            const hint = document.getElementById('upAccountHint');
            if (hint) {
                if (emptyGroup) {
                    hint.textContent = `Grup ${uploadPlatform} masih kosong - masukkan akun ke grup itu lewat kartu profil di tab Profil.`;
                } else if (filtered) {
                    hint.textContent = `Difilter grup ${uploadPlatform}: ${profiles.length} akun`;
                } else {
                    hint.textContent = 'Semua akun (belum ada grup yang cocok dengan platform ini)';
                }
                hint.classList.remove('view-hidden');
            }
        }

        function openUploadFor(profileName, platform = 'youtube') {
            setUploadPlatform(platform);
            switchView('uploader');
            const subBtn = document.querySelector(`#uploaderSubNav .sub-tab-btn[data-platform="${platform}"]`);
            if (subBtn) {
                document.querySelectorAll('#uploaderSubNav .sub-tab-btn').forEach(b => b.classList.remove('active'));
                subBtn.classList.add('active');
            }
            populateUploadAccounts(profileName);
            if (platform === 'youtube') loadCachedChannels(profileName);
        }

        // Native file picker (mode desktop pywebview); fallback: ketik manual
        document.querySelectorAll('.btn-browse').forEach(btn => {
            btn.onclick = async () => {
                const target = document.getElementById(btn.dataset.target);
                if (window.pywebview && window.pywebview.api && window.pywebview.api.pick_file) {
                    try {
                        const path = await window.pywebview.api.pick_file(btn.dataset.kind);
                        if (path) target.value = path;
                    } catch (e) { showToast('Gagal membuka dialog file', 'error'); }
                } else {
                    target.focus();
                    showToast('Ketik path file secara manual di mode browser', 'info');
                }
            };
        });

        // ---------- Mode Multi Akun (TikTok): N akun upload bersamaan ----------
        let multiJobs = [];
        let multiPollTimer = null;

        function renderMultiAccounts() {
            const wrap = document.getElementById('multiAccountList');
            if (!wrap) return;
            // simpan pilihan/CSV yang sudah diisi sebelum re-render
            const prev = {};
            wrap.querySelectorAll('.multi-acc-row').forEach(r => {
                const channelEl = r.querySelector('.multiAccChannel');
                const pageEl = r.querySelector('.multiAccPage');
                const pageManualEl = r.querySelector('.multiAccPageManual');
                const groupEl = r.querySelector('.multiAccGroupSel');
                const groupManualEl = r.querySelector('.multiAccGroupId');
                prev[r.dataset.profile] = {
                    checked: r.querySelector('.multiAccSel').checked,
                    csv: r.querySelector('.multiAccCsv').value,
                    channel: channelEl ? channelEl.value : undefined,
                    page: pageEl ? pageEl.value : undefined,
                    pageManual: pageManualEl ? pageManualEl.value : '',
                    group: groupEl ? groupEl.value : undefined,
                    groupManual: groupManualEl ? groupManualEl.value : '',
                };
            });
            const { profiles } = profilesForPlatform(uploadPlatform);
            wrap.innerHTML = '';
            if (!profiles.length) {
                wrap.innerHTML = '<p class="sb-muted">Belum ada akun untuk platform ini. Tambahkan profil ke grup platform ini di tab Profil.</p>';
                return;
            }
            const isYt = uploadPlatform === 'youtube';
            const isFb = uploadPlatform === 'facebook';
            const fbTarget = document.getElementById('fbTarget').value;
            const showFbPages = isFb && fbTarget === 'page';
            const showFbGroups = isFb && fbTarget === 'group';
            profiles.forEach(p => {
                const row = document.createElement('div');
                row.className = 'multi-acc-row';
                row.dataset.profile = p.name;
                const short = getUploadAccountLabel(p, uploadPlatform);
                // Khusus YouTube: tampilkan channel MANA yang akan dipakai per
                // akun (satu gmail bisa punya beberapa channel) - supaya user
                // tidak bingung/salah channel saat upload bersamaan banyak akun.
                const channelHtml = isYt ? `
                    <div class="file-pick multi-acc-channel">
                        <select class="modal-select multiAccChannel" title="Channel YouTube untuk akun ini"></select>
                        <button type="button" class="btn-secondary multi-acc-detect" title="Deteksi channel di gmail ini"><i class="fa-solid fa-magnifying-glass"></i></button>
                    </div>` : '';
                const pageHtml = showFbPages ? `
                    <div class="multi-acc-channel">
                        <div class="file-pick">
                            <select class="modal-select multiAccPage" title="Fanpage Facebook untuk akun ini"></select>
                            <button type="button" class="btn-secondary multi-acc-detect-page" title="Deteksi fanpage di akun ini"><i class="fa-solid fa-magnifying-glass"></i></button>
                        </div>
                        <input type="text" class="up-input multiAccPageManual" placeholder="Atau isi manual page ID">
                    </div>` : '';
                const groupHtml = showFbGroups ? `
                    <div class="multi-acc-channel">
                        <div class="file-pick">
                            <select class="modal-select multiAccGroupSel" title="Grup Facebook untuk akun ini"></select>
                            <button type="button" class="btn-secondary multi-acc-detect-group" title="Deteksi grup di akun ini"><i class="fa-solid fa-magnifying-glass"></i></button>
                        </div>
                        <input type="text" class="up-input multiAccGroupId" placeholder="Atau isi manual Group ID">
                    </div>` : '';
                row.innerHTML = `
                    <label class="multi-acc-check">
                        <input type="checkbox" class="multiAccSel">
                        <span title="${escapeHtml(p.name)}">${escapeHtml(short)}</span>
                    </label>
                    ${channelHtml}
                    ${pageHtml}
                    ${groupHtml}
                    <div class="file-pick multi-acc-csv">
                        <input type="text" class="up-input multiAccCsv" placeholder="File CSV untuk akun ini...">
                        <button type="button" class="btn-secondary" title="Pilih file CSV"><i class="fa-solid fa-folder-open"></i></button>
                    </div>`;
                if (isYt) {
                    const sel = row.querySelector('.multiAccChannel');
                    const fillRowChannels = (channels, active) => {
                        sel.innerHTML = '';
                        sel.add(new Option('Channel default (aktif)', ''));
                        (channels || []).forEach(c => {
                            const opt = new Option(channelLabel(c), channelValue(c));
                            if (c.active) opt.selected = true;
                            sel.add(opt);
                        });
                        if (active) {
                            for (const o of sel.options) {
                                if (o.value && o.value.toLowerCase() === active.toLowerCase()) { o.selected = true; break; }
                            }
                        }
                    };
                    fillRowChannels(p.channels);
                    row.querySelector('.multi-acc-detect').onclick = () => {
                        showToast(`Mendeteksi channel ${short}... (bisa 20-40 detik)`, 'info');
                        api('/api/youtube/channels', 'POST', { profile: p.name }).then(data => {
                            if (!data.success) { showToast(data.error || 'Gagal', 'error'); return; }
                            const poll = setInterval(() => {
                                fetch(`/api/youtube/job/${data.job_id}`).then(r => r.json()).then(s => {
                                    if (!s.done) return;
                                    clearInterval(poll);
                                    if (s.success) {
                                        const chans = s.channels || [];
                                        fillRowChannels(chans, s.active);
                                        api('/api/youtube/channels/save', 'POST', { profile: p.name, channels: chans });
                                        p.channels = chans;
                                        showToast(`${short}: ${chans.length} channel ditemukan`, 'success');
                                    } else {
                                        showToast('Deteksi gagal: ' + (s.error || ''), 'error');
                                    }
                                }).catch(() => {});
                            }, 2000);
                        }).catch(() => showToast('Gagal terhubung ke server', 'error'));
                    };
                }
                if (showFbPages) {
                    const sel = row.querySelector('.multiAccPage');
                    const manual = row.querySelector('.multiAccPageManual');
                    const fillRowPages = (pages, selected = '') => fillPageSelect(sel, pages, selected);
                    fillRowPages(p.fb_pages || []);
                    sel.addEventListener('change', () => {
                        if (sel.value) manual.value = '';
                    });
                    manual.addEventListener('input', () => {
                        if (manual.value.trim()) sel.value = '';
                    });
                    row.querySelector('.multi-acc-detect-page').onclick = () => {
                        showToast(`Mendeteksi fanpage ${short}... (bisa 20-40 detik)`, 'info');
                        api('/api/facebook/pages', 'POST', {
                            profile: p.name,
                            headless: !!document.getElementById('upInvisible').checked
                        }).then(data => {
                            if (!data.success) { showToast(data.error || 'Gagal', 'error'); return; }
                            const poll = setInterval(() => {
                                fetch(`/api/youtube/job/${data.job_id}`).then(r => r.json()).then(s => {
                                    if (!s.done) return;
                                    clearInterval(poll);
                                    if (s.success) {
                                        const pages = s.pages || [];
                                        p.fb_pages = pages;
                                        fillRowPages(pages, manual.value.trim() || sel.value);
                                        api('/api/facebook/pages/save', 'POST', { profile: p.name, pages });
                                        showToast(`${short}: ${pages.length} fanpage ditemukan`, pages.length ? 'success' : 'info');
                                    } else {
                                        showToast('Deteksi gagal: ' + (s.error || ''), 'error');
                                    }
                                }).catch(() => {});
                            }, 2000);
                        }).catch(() => showToast('Gagal terhubung ke server', 'error'));
                    };
                }
                if (showFbGroups) {
                    const sel = row.querySelector('.multiAccGroupSel');
                    const manual = row.querySelector('.multiAccGroupId');
                    const fillRowGroups = (groups, selected = '') => fillGroupSelect(sel, groups, selected);
                    fillRowGroups(p.fb_groups || []);
                    sel.addEventListener('change', () => {
                        if (sel.value) manual.value = '';
                    });
                    manual.addEventListener('input', () => {
                        if (manual.value.trim()) sel.value = '';
                    });
                    row.querySelector('.multi-acc-detect-group').onclick = () => {
                        showToast(`Mendeteksi grup ${short}... (bisa 20-40 detik)`, 'info');
                        fetch('/api/facebook/groups', {method:'POST', headers:{'Content-Type':'application/json'},
                            body: JSON.stringify({profile: p.name, cached_only: false})})
                            .then(r => r.json()).then(data => {
                                if (!data.job_id) { showToast(data.error || 'Gagal memulai deteksi grup', 'error'); return; }
                                const poll = setInterval(() => {
                                    fetch(`/api/youtube/job/${data.job_id}`).then(r => r.json()).then(s => {
                                        if (!s.done) return;
                                        clearInterval(poll);
                                        const groups = s.groups || [];
                                        showToast(`${short}: ${groups.length} grup ditemukan`, groups.length ? 'success' : 'info');
                                        if (groups.length) {
                                            fetch('/api/facebook/groups/save', {method:'POST', headers:{'Content-Type':'application/json'},
                                                body: JSON.stringify({profile: p.name, groups})});
                                            fillRowGroups(groups, manual.value.trim() || sel.value);
                                            p.fb_groups = groups;
                                        }
                                    }).catch(() => clearInterval(poll));
                                }, 2000);
                            }).catch(() => showToast('Gagal terhubung ke server', 'error'));
                    };
                }
                const saved = prev[p.name];
                if (saved) {
                    row.querySelector('.multiAccSel').checked = saved.checked;
                    row.querySelector('.multiAccCsv').value = saved.csv;
                    if (isYt && saved.channel !== undefined) row.querySelector('.multiAccChannel').value = saved.channel;
                    if (showFbPages) {
                        if (saved.page !== undefined) row.querySelector('.multiAccPage').value = saved.page;
                        row.querySelector('.multiAccPageManual').value = saved.pageManual || '';
                    }
                    if (showFbGroups) {
                        if (saved.group !== undefined) row.querySelector('.multiAccGroupSel').value = saved.group;
                        row.querySelector('.multiAccGroupId').value = saved.groupManual || '';
                    }
                }
                row.querySelector('.multi-acc-csv button').onclick = async () => {
                    const input = row.querySelector('.multiAccCsv');
                    if (window.pywebview && window.pywebview.api && window.pywebview.api.pick_file) {
                        try {
                            const path = await window.pywebview.api.pick_file('csv');
                            if (path) input.value = path;
                        } catch (e) { showToast('Gagal membuka dialog file', 'error'); }
                    } else {
                        input.focus();
                        showToast('Ketik path CSV manual di mode browser', 'info');
                    }
                };
                wrap.appendChild(row);
            });
        }

        function setUploadStopButtons(singleActive = false, multiActive = false) {
            btnStopMultiUpload.classList.toggle('view-hidden', !multiActive);
            btnStopUpload.disabled = !singleActive;
            btnStopMultiUpload.disabled = !multiActive;
        }

        async function stopSingleUpload(showNotice = true) {
            const jobId = activeUploadJobId;
            if (!jobId) return;
            btnStopUpload.disabled = true;
            try {
                const res = await api(`/api/youtube/job/${jobId}/stop`, 'POST', {});
                if (showNotice) showToast(res.message || 'Permintaan stop dikirim', res.success ? 'info' : 'error');
            } catch (e) {
                if (showNotice) showToast('Gagal mengirim perintah stop', 'error');
            }
        }

        async function stopMultiUpload(showNotice = true) {
            const active = multiJobs.filter(j => j.job_id && !j.done);
            if (!active.length) return;
            btnStopMultiUpload.disabled = true;
            await Promise.all(active.map(async j => {
                try {
                    const res = await api(`/api/youtube/job/${j.job_id}/stop`, 'POST', {});
                    j.msg = (res && res.message) || 'stop diminta';
                } catch (e) {
                    j.msg = 'gagal kirim stop';
                }
            }));
            renderMultiProgress();
            if (showNotice) showToast('Perintah stop dikirim ke semua upload aktif', 'info');
        }

        function stopUploadJobsOnClose() {
            if (activeUploadJobId) {
                try {
                    navigator.sendBeacon(`/api/youtube/job/${activeUploadJobId}/stop`, new Blob(['{}'], { type: 'application/json' }));
                } catch (e) {}
            }
            multiJobs.filter(j => j.job_id && !j.done).forEach(j => {
                try {
                    navigator.sendBeacon(`/api/youtube/job/${j.job_id}/stop`, new Blob(['{}'], { type: 'application/json' }));
                } catch (e) {}
            });
        }

        btnStopUpload.onclick = () => { stopSingleUpload(true); };
        btnStopMultiUpload.onclick = () => { stopMultiUpload(true); };
        window.addEventListener('pagehide', stopUploadJobsOnClose);
        window.addEventListener('beforeunload', stopUploadJobsOnClose);

        function renderMultiProgress() {
            const box = document.getElementById('multiProgress');
            box.innerHTML = multiJobs.map(j => {
                const cls = j.done ? (j.success ? ' ok' : ' fail') : '';
                const badge = j.done ? (j.success ? '&#10004;' : '&#10008;') : (j.pct + '%');
                return `<div class="multi-prog-row${cls}">
                    <span class="mp-name" title="${escapeHtml(j.profile)}">${escapeHtml(j.profile.replace('_gmail.com', ''))}</span>
                    <div class="progress-track"><div class="progress-fill" style="width:${Math.max(0, Math.min(100, j.pct))}%"></div></div>
                    <span class="mp-pct">${badge}</span>
                    <span class="mp-msg">${escapeHtml(j.msg || '')}</span>
                </div>`;
            }).join('');
        }

        function pollMultiJobs() {
            const active = multiJobs.filter(j => j.job_id && !j.done);
            if (!active.length) {
                clearInterval(multiPollTimer);
                multiPollTimer = null;
                btnStartUpload.disabled = false;
                setUploadStopButtons(!!activeUploadJobId, false);
                const ok = multiJobs.filter(j => j.success).length;
                showToast(`Multi upload selesai: ${ok}/${multiJobs.length} akun sukses`,
                          ok === multiJobs.length ? 'success' : 'error');
                return;
            }
            active.forEach(j => {
                fetch(`/api/youtube/job/${j.job_id}`).then(r => r.json()).then(s => {
                    j.pct = s.pct || 0;
                    let msg = s.message || '';
                    if (s.rows && s.rows.length) {
                        const doneRows = s.rows.filter(r => r.status === 'completed').length;
                        msg = `${doneRows}/${s.rows.length} video | ` + msg;
                    }
                    j.msg = msg;
                    if (s.done) {
                        j.done = true;
                        j.success = !!s.success;
                        j.pct = s.success ? 100 : j.pct;
                        j.msg = s.success ? 'Selesai' : (s.error || 'Gagal');
                    }
                    renderMultiProgress();
                }).catch(() => {});
            });
        }

        function startMultiUpload(rows, extras) {
            const invisible = document.getElementById('upInvisible').checked;
            btnStartUpload.disabled = true;
            multiJobs = rows.map(r => ({
                profile: r.profile, csv: r.csv, job_id: null,
                channel: r.channel || '',
                page_id: r.page_id || '',
                page_name: r.page_name || '',
                group_id: r.group_id || '',
                group_name: r.group_name || '',
                pct: 0, msg: 'memulai...', done: false, success: false,
            }));
            revealProgressPanel('multiProgress');
            setUploadStopButtons(false, false);
            renderMultiProgress();
            Promise.all(multiJobs.map(j =>
                api('/api/youtube/upload', 'POST', Object.assign({}, extras || {}, {
                    profile: j.profile,
                    platform: uploadPlatform,
                    mode: 'batch',
                    csv_path: j.csv,
                    channel_id: j.channel || '',
                    page_id: j.page_id || '',
                    page_name: j.page_name || '',
                    group_id: j.group_id || '',
                    group_name: j.group_name || '',
                    invisible,
                })).then(d => {
                    if (d.success) { j.job_id = d.job_id; j.msg = 'antre...'; }
                    else { j.done = true; j.msg = d.error || 'Gagal memulai'; }
                }).catch(() => { j.done = true; j.msg = 'Gagal terhubung ke server'; })
            )).then(() => {
                renderMultiProgress();
                const started = multiJobs.filter(j => j.job_id).length;
                if (!started) { btnStartUpload.disabled = false; showToast('Tidak ada job yang berhasil dimulai', 'error'); return; }
                showToast(`${started} akun mulai upload bersamaan - jangan tutup aplikasi`, 'info');
                setUploadStopButtons(false, true);
                if (multiPollTimer) clearInterval(multiPollTimer);
                multiPollTimer = setInterval(pollMultiJobs, 2000);
            });
        }

        // Kumpulkan pengaturan platform-level (berlaku Batch & Multi Akun).
        // Kembalikan objek param, atau null bila ada yang wajib belum diisi.
        function collectPlatformExtras() {
            const P = uploadPlatform;
            const extras = {};
            if (P === 'tiktok') {
                extras.upload_type = document.getElementById('upType').value;
                extras.delete_after = document.getElementById('upDeleteAfter').checked;
                // Gunakan lokasi dari CSV (hanya mode regular)
                if (extras.upload_type === 'regular' && document.getElementById('upTtUseLocation').checked) {
                    extras.use_location = true;
                }
                // AI Generate Content (global override untuk semua video di run ini)
                if (document.getElementById('upTtAiGenerate').checked) extras.ai_generate_content = true;
            }
            if (P === 'youtube') {
                extras.yt_type = document.getElementById('upYtType').value;   // short | long
                extras.privacy = document.getElementById('upPrivacy').value;
                if (document.getElementById('upAiContent').checked) extras.force_ai_content = true;
                if (document.getElementById('upMadeForKids').checked) extras.force_made_for_kids = true;
            }
            if (P === 'facebook') {
                extras.target = document.getElementById('fbTarget').value;
                if (extras.target === 'page') {
                    const selectedPageId = document.getElementById('fbPage').value || '';
                    const manualPageId = document.getElementById('fbPageManual').value.trim();
                    extras.page_id = selectedPageId || manualPageId;
                    if (!extras.page_id) { showToast('Pilih atau ketik Fanpage dulu', 'error'); return null; }
                    if (!isNumericFbPageId(extras.page_id)) {
                        showToast('Fanpage ID harus angka. Klik Deteksi Fanpage lalu pilih dari dropdown.', 'error');
                        return null;
                    }
                    const pageInfo = findPageMeta((getProfileByName(upAccount.value) || {}).fb_pages || [], extras.page_id);
                    extras.page_name = pageInfo ? String(pageInfo.name || '').trim() : '';
                }
                if (extras.target === 'group') {
                    const selectedGroupId = document.getElementById('fbGroup').value || '';
                    const manualGroupId = document.getElementById('fbGroupManual').value.trim();
                    extras.group_id = selectedGroupId || manualGroupId;
                    if (!extras.group_id) { showToast('Pilih atau ketik Group ID dulu', 'error'); return null; }
                    const profileData = getProfileByName(upAccount.value) || {};
                    const groupList = profileData.fb_groups || [];
                    const gInfo = groupList.find(g => String(g.id||'') === extras.group_id);
                    extras.group_name = gInfo ? String(gInfo.name || '').trim() : '';
                }
                // Ambil publish_mode dan post_delay (hanya untuk personal/page)
                if (extras.target !== 'group') {
                    extras.publish_mode = document.getElementById('fbPublishMode').value;
                    extras.post_delay = parseInt(document.getElementById('fbPostDelay').value) || 0;
                } else {
                    extras.post_delay = parseInt(document.getElementById('fbPostDelay').value) || 0;
                }
            }
            return extras;
        }

        btnStartUpload.onclick = () => {
            const av = platformAvail[uploadPlatform];
            if (av && !av.available) { showToast('Engine platform ini belum siap', 'error'); return; }

            const extras = collectPlatformExtras();
            if (extras === null) return;   // validasi platform gagal (mis. fanpage kosong)

            if (uploadMode === 'multi') {
                const rows = [...document.querySelectorAll('#multiAccountList .multi-acc-row')]
                    .filter(r => r.querySelector('.multiAccSel').checked)
                    .map(r => {
                        const channelEl = r.querySelector('.multiAccChannel');
                        const pageEl = r.querySelector('.multiAccPage');
                        const pageManualEl = r.querySelector('.multiAccPageManual');
                        const groupSelEl = r.querySelector('.multiAccGroupSel');
                        const groupManualEl = r.querySelector('.multiAccGroupId');
                        const chosenPageId = (pageEl ? pageEl.value : '') || (pageManualEl ? pageManualEl.value.trim() : '');
                        const chosenGroupId = (groupSelEl ? groupSelEl.value : '') || (groupManualEl ? groupManualEl.value.trim() : '');
                        const pageInfo = findPageMeta((getProfileByName(r.dataset.profile) || {}).fb_pages || [], chosenPageId);
                        const groupInfo = findGroupMeta((getProfileByName(r.dataset.profile) || {}).fb_groups || [], chosenGroupId);
                        return {
                            profile: r.dataset.profile,
                            csv: r.querySelector('.multiAccCsv').value.trim(),
                            channel: channelEl ? channelEl.value : '',
                            page_id: chosenPageId,
                            page_name: pageInfo ? String(pageInfo.name || '').trim() : '',
                            group_id: chosenGroupId,
                            group_name: groupInfo ? String(groupInfo.name || '').trim() : '',
                        };
                    });
                if (!rows.length) { showToast('Ceklis minimal 1 akun dulu', 'error'); return; }
                const missing = rows.filter(r => !r.csv);
                if (missing.length) {
                    showToast(`Isi CSV untuk: ${missing.map(r => r.profile.replace('_gmail.com', '')).join(', ')}`, 'error');
                    return;
                }
                if (uploadPlatform === 'facebook' && extras.target === 'page') {
                    const missingPage = rows.filter(r => !(r.page_id || '').trim());
                    if (missingPage.length) {
                        showToast(`Pilih atau isi fanpage untuk: ${missingPage.map(r => r.profile.replace('_gmail.com', '')).join(', ')}`, 'error');
                        return;
                    }
                    const invalidPage = rows.filter(r => !isNumericFbPageId(r.page_id));
                    if (invalidPage.length) {
                        showToast(`Fanpage ID harus angka untuk: ${invalidPage.map(r => r.profile.replace('_gmail.com', '')).join(', ')}`, 'error');
                        return;
                    }
                }
                if (uploadPlatform === 'facebook' && extras.target === 'group') {
                    // Pakai group_id dari extras jika row tidak punya sendiri
                    rows.forEach(r => { if (!r.group_id) r.group_id = extras.group_id || ''; });
                    const missingGroup = rows.filter(r => !(r.group_id || '').trim());
                    if (missingGroup.length) {
                        showToast(`Isi Group ID untuk: ${missingGroup.map(r => r.profile.replace('_gmail.com', '')).join(', ')}`, 'error');
                        return;
                    }
                }
                startMultiUpload(rows, extras);
                return;
            }

            const profile = upAccount.value;
            if (!profile) { showToast('Pilih akun dulu', 'error'); return; }

            const csv_path = document.getElementById('upCsv').value.trim();
            if (!csv_path) { showToast('Isi path CSV dulu', 'error'); return; }

            let payload = Object.assign({
                profile,
                platform: uploadPlatform,
                mode: 'batch',
                channel_id: upChannel.value,
                csv_path,
                invisible: document.getElementById('upInvisible').checked,
            }, extras);

            // Jika pratinjau sudah dimuat, hanya upload baris yang dicentang
            const totalRows = document.querySelectorAll('#csvPreview .rowSel').length;
            if (totalRows > 0) {
                const selected = getSelectedCsvRows();
                if (!selected.length) { showToast('Pilih minimal 1 item untuk diupload', 'error'); return; }
                payload.selected_rows = selected;
            }

            btnStartUpload.disabled = true;
            activeUploadJobId = '';
            setUploadStopButtons(false, false);
            setUploadProgress(0, 'Memulai...', 'Menyiapkan worker upload...');
            revealProgressPanel('uploadProgress');

            api('/api/youtube/upload', 'POST', payload).then(data => {
                if (!data.success) {
                    showToast(data.error || 'Gagal memulai upload', 'error');
                    setUploadProgress(0, 'Gagal', data.error || '');
                    btnStartUpload.disabled = false;
                    return;
                }
                showToast('Upload dimulai - jangan tutup aplikasi', 'info');
                activeUploadJobId = data.job_id || '';
                setUploadStopButtons(!!activeUploadJobId, false);
                pollJob(data.job_id);
            }).catch(() => {
                showToast('Gagal terhubung ke server', 'error');
                btnStartUpload.disabled = false;
                setUploadStopButtons(false, false);
            });
        };

        function pollJob(jobId) {
            clearInterval(uploadPolling);
            uploadPolling = setInterval(() => {
                fetch(`/api/youtube/job/${jobId}`).then(r => r.json()).then(s => {
                    if (s.error && !s.message) { /* keep polling briefly */ }
                    let msg = s.message || '';
                    if (s.rows && s.rows.length) {
                        msg += '\n' + s.rows.map((r, i) => `${i+1}. ${r.title}: ${r.status}`).join('\n');
                    }
                    setUploadProgress(s.pct || 0, s.pct ? s.pct + '%' : '...', msg);
                    if (s.done) {
                        clearInterval(uploadPolling);
                        uploadPolling = null;
                        btnStartUpload.disabled = false;
                        activeUploadJobId = '';
                        setUploadStopButtons(false, multiJobs.some(j => j.job_id && !j.done));
                        if (s.success) {
                            setUploadProgress(100, 'Selesai', 'Upload berhasil!');
                            const platformLabel = (PLATFORM_META[uploadPlatform]?.label || 'Upload').replace('Upload ', '');
                            showToast('Upload ' + platformLabel + ' berhasil!', 'success');
                        } else {
                            setUploadProgress(s.pct || 0, 'Gagal', s.error || 'Upload gagal');
                            showToast('Upload gagal: ' + (s.error || ''), 'error');
                        }
                    }
                }).catch(() => {});
            }, 1500);
        }

        function setUploadProgress(pct, label, msg) {
            document.getElementById('upProgFill').style.width = Math.max(0, Math.min(100, pct)) + '%';
            document.getElementById('upProgPct').textContent = label;
            document.getElementById('upProgLabel').textContent = pct >= 100 ? 'Selesai' : 'Sedang upload';
            document.getElementById('upProgMsg').textContent = msg || '';
        }

        function escapeHtml(s) {
            const div = document.createElement('div');
            div.textContent = s;
            return div.innerHTML;
        }

        function linkifyText(text) {
            const src = String(text || '');
            const escaped = escapeHtml(src);
            const urlRe = /((https?:\/\/|www\.)[^\s<]+)/gi;
            return escaped.replace(urlRe, (raw) => {
                const href = raw.toLowerCase().startsWith('www.') ? `https://${raw}` : raw;
                return `<a href="${href}" class="chat-link" target="_blank" rel="noopener noreferrer">${raw}</a>`;
            }).replace(/\n/g, '<br>');
        }

        let toastTimer = null;
        function showToast(message, type) {
            toast.textContent = message;
            toast.className = `toast show ${type}`;
            clearTimeout(toastTimer);
            toastTimer = setTimeout(() => {
                toast.className = toast.className.replace('show', '');
            }, 3000);
        }

        // ---------- Settings Management ----------
        const omnivoraSettingsModal = document.getElementById('omnivoraSettingsModal');
        const settingsAiProvider = document.getElementById('settingsAiProvider');
        const settingsGeminiKey = document.getElementById('settingsGeminiKey');
        const settingsGroqKey = document.getElementById('settingsGroqKey');

        // Key bisa dipisah baris (format baru) ATAU koma (data lama) - dua-duanya dibaca.
        function parseApiKeys(text) {
            return (text || '').split(/[\n,]+/).map(s => s.trim()).filter(Boolean);
        }

        document.getElementById('btnOmnivoraSettings').onclick = () => {
            api('/api/settings', 'GET')
                .then(cfg => {
                    settingsAiProvider.value = cfg.ai_provider || 'Gemini';
                    settingsGeminiKey.value = (cfg.gemini_api_keys || [cfg.gemini_api_key || '']).filter(Boolean).join('\n');
                    settingsGroqKey.value = (cfg.groq_api_keys || []).filter(Boolean).join('\n');
                    omnivoraSettingsModal.classList.add('open');
                })
                .catch(() => showToast('Gagal memuat pengaturan', 'error'));
        };

        // Tombol link "dapatkan API key" -> buka di browser sistem (pywebview
        // tidak membuka <a> ke browser default, jadi lewat backend).
        document.querySelectorAll('.apikey-link').forEach(btn => {
            btn.onclick = () => {
                const url = btn.dataset.url;
                api('/api/open_url', 'POST', { url }).then(d => {
                    if (!d.success) window.open(url, '_blank');
                }).catch(() => window.open(url, '_blank'));
            };
        });

        const closeSettings = () => {
            omnivoraSettingsModal.classList.remove('open');
        };
        document.getElementById('btnCancelOmnivoraSettings').onclick = closeSettings;
        document.getElementById('btnCloseOmnivoraSettings').onclick = closeSettings;
        omnivoraSettingsModal.addEventListener('click', (e) => {
            if (e.target === omnivoraSettingsModal) closeSettings();
        });

        document.getElementById('btnSaveOmnivoraSettings').onclick = () => {
            const geminiKeys = parseApiKeys(settingsGeminiKey.value);
            const data = {
                ai_provider: settingsAiProvider.value,
                gemini_api_key: geminiKeys[0] || '',
                gemini_api_keys: geminiKeys,
                groq_api_keys: parseApiKeys(settingsGroqKey.value),
            };
            api('/api/settings/save', 'POST', data)
                .then(res => {
                    if (res.success) {
                        showToast('Pengaturan disimpan!', 'success');
                        closeSettings();
                    } else {
                        showToast(res.error || 'Gagal menyimpan', 'error');
                    }
                })
                .catch(() => showToast('Gagal menyimpan pengaturan', 'error'));
        };

        // ---------- Riset View ----------
        let risetInitialized = false;
        let activeRisetPlatform = 'shopee';
        let populateRisetProfiles = null;
        const RISET_PLATFORM_UI = {
            shopee: {
                label: 'Shopee',
                icon: 'fa-solid fa-bag-shopping',
                title: 'Riset Produk Shopee',
                hint: 'Cari produk affiliate, lihat harga dan performa, lalu tarik langsung ke Product Scraper.',
                placeholder: 'Contoh: baju gamis wanita, sepatu olahraga...',
                loading: 'Mencari produk di Shopee...',
                note: 'Shopee mode: riset dibuka lewat akun login terpilih, cocok untuk cek hasil pencarian, seller, dan dashboard affiliate dari profile itu sendiri.',
                action: 'Aksi cepat: buka search Shopee, dashboard affiliate, atau query Google melalui profile login yang dipilih user.',
                empty: 'Belum ada jalur riset Shopee yang bisa dibuka.',
                button: 'Siapkan Jalur Riset',
                supportsRegion: true,
            },
            tiktok: {
                label: 'TikTok',
                icon: 'fa-brands fa-tiktok',
                title: 'Riset TikTok',
                hint: 'Bangun jalur riset untuk hook, hashtag, angle video, dan pola konten yang lagi naik di TikTok.',
                placeholder: 'Contoh: tas wanita viral, skincare glowing, konten affiliate lucu...',
                loading: 'Menyiapkan jalur riset TikTok...',
                note: 'TikTok mode: fokus ke discovery konten, hashtag, creator, dan phrasing yang berpotensi FYP.',
                action: 'Aksi cepat: buka search, hashtag, dan Google site query untuk membandingkan angle konten.',
                empty: 'Jalur riset TikTok belum tersedia.',
                button: 'Buka Jalur Riset',
                supportsRegion: false,
            },
            youtube: {
                label: 'YouTube',
                icon: 'fa-brands fa-youtube',
                title: 'Riset YouTube',
                hint: 'Cari ide video, format thumbnail, judul, dan topik evergreen per keyword.',
                placeholder: 'Contoh: review parfum lokal, resep mie viral, ide shorts jualan...',
                loading: 'Menyiapkan jalur riset YouTube...',
                note: 'YouTube mode: cocok untuk membaca pola judul, niche content gap, dan format Shorts maupun video panjang.',
                action: 'Aksi cepat: buka search, hashtag, dan Google site query untuk validasi kompetitor.',
                empty: 'Jalur riset YouTube belum tersedia.',
                button: 'Buka Jalur Riset',
                supportsRegion: false,
            },
            facebook: {
                label: 'Facebook',
                icon: 'fa-brands fa-facebook-f',
                title: 'Riset Facebook',
                hint: 'Petakan fanpage, posting, reels, dan percakapan audience untuk niche yang kamu incar.',
                placeholder: 'Contoh: jualan gamis syari, herbal ibu hamil, promo skincare...',
                loading: 'Menyiapkan jalur riset Facebook...',
                note: 'Facebook mode: cocok untuk cari fanpage, percakapan pasar, dan gaya copy yang lebih direct-response.',
                action: 'Aksi cepat: buka search native dan query Google untuk memetakan page, grup, dan posting publik.',
                empty: 'Jalur riset Facebook belum tersedia.',
                button: 'Buka Jalur Riset',
                supportsRegion: false,
            },
            instagram: {
                label: 'Instagram',
                icon: 'fa-brands fa-instagram',
                title: 'Riset Instagram',
                hint: 'Eksplor akun, hashtag, dan visual angle untuk niche konten maupun promosi produk.',
                placeholder: 'Contoh: outfit kantor hijab, parfum lokal cewek, dekor kamar estetik...',
                loading: 'Menyiapkan jalur riset Instagram...',
                note: 'Instagram mode: fokus pada hashtag, akun niche, dan pola visual yang berulang di konten perform tinggi.',
                action: 'Aksi cepat: buka search, halaman hashtag, dan query Google untuk akun serta posting niche.',
                empty: 'Jalur riset Instagram belum tersedia.',
                button: 'Buka Jalur Riset',
                supportsRegion: false,
            },
            pinterest: {
                label: 'Pinterest',
                icon: 'fa-brands fa-pinterest',
                title: 'Riset Pinterest',
                hint: 'Temukan angle visual, board kompetitor, dan intent pencarian yang kuat untuk image-led content.',
                placeholder: 'Contoh: ide ruang tamu minimalis, resep dessert box, poster promo skincare...',
                loading: 'Menyiapkan jalur riset Pinterest...',
                note: 'Pinterest mode: bagus untuk kebutuhan visual campaign, storyboard, thumbnail, dan landing pin.',
                action: 'Aksi cepat: buka pencarian pin, board, dan query Google untuk menggali pola visual.',
                empty: 'Jalur riset Pinterest belum tersedia.',
                button: 'Buka Jalur Riset',
                supportsRegion: false,
            },
            threads: {
                label: 'Threads',
                icon: 'fa-brands fa-threads',
                title: 'Riset Threads',
                hint: 'Cari obrolan, opini, dan bahasa audience untuk ide konten yang lebih natural dan conversational.',
                placeholder: 'Contoh: kerja remote pemula, tips jualan online, review serum viral...',
                loading: 'Menyiapkan jalur riset Threads...',
                note: 'Threads mode: cocok untuk membaca problem, opini, dan angle copy yang terasa lebih manusiawi.',
                action: 'Aksi cepat: buka search native dan query insight untuk pertanyaan audience.',
                empty: 'Jalur riset Threads belum tersedia.',
                button: 'Buka Jalur Riset',
                supportsRegion: false,
            },
            x: {
                label: 'X',
                icon: 'fa-brands fa-x-twitter',
                title: 'Riset X',
                hint: 'Pantau percakapan real-time, opini, hashtag, dan respon pasar untuk topik yang bergerak cepat.',
                placeholder: 'Contoh: gadget budget 2026, promo affiliate, skincare acne journey...',
                loading: 'Menyiapkan jalur riset X...',
                note: 'X mode: efektif untuk melihat momentum topik, opini tajam, dan reaksi audience secara real-time.',
                action: 'Aksi cepat: buka live search, hashtag query, dan Google site search untuk riset cepat.',
                empty: 'Jalur riset X belum tersedia.',
                button: 'Buka Jalur Riset',
                supportsRegion: false,
            },
        };
        function initRisetView() {
            if (risetInitialized) return;
            risetInitialized = true;

            const btnSearch = document.getElementById('btnRisetSearch');
            const profileSelect = document.getElementById('risetProfile');
            const keywordInput = document.getElementById('risetKeyword');
            const regionSelect = document.getElementById('risetRegion');
            const regionWrap = document.getElementById('risetRegionWrap');
            const loadingPanel = document.getElementById('risetLoading');
            const loadingText = document.getElementById('risetLoadingText');
            const resultsGrid = document.getElementById('risetResultsGrid');
            const titleEl = document.getElementById('risetTitle');
            const hintEl = document.getElementById('risetHint');
            const badgeEl = document.getElementById('risetBadge');
            const noteEl = document.getElementById('risetPlatformNote');
            const actionEl = document.getElementById('risetActionNote');
            const platformBar = document.getElementById('risetPlatformBar');

            populateRisetProfiles = function () {
                const current = profileSelect.value;
                const { profiles, filtered, emptyGroup } = profilesForPlatform(activeRisetPlatform);
                profileSelect.innerHTML = '';
                const cfg = RISET_PLATFORM_UI[activeRisetPlatform] || {};
                const label = cfg.label || activeRisetPlatform;
                profileSelect.add(new Option(emptyGroup
                    ? `Grup ${label} masih kosong - isi dulu di tab Profil`
                    : filtered
                        ? `Pilih akun (grup ${label}: ${profiles.length})...`
                        : 'Pilih akun login...', ''));
                profiles.forEach(p => {
                    profileSelect.add(new Option(p.name.replace('_gmail.com', ''), p.name));
                });
                profileSelect.title = emptyGroup
                    ? `Grup ${label} belum punya anggota - masukkan akun ke grup itu lewat kartu profil`
                    : filtered
                        ? `Difilter: hanya akun dari grup yang cocok dengan ${label}`
                        : 'Belum ada grup yang cocok dengan platform ini - menampilkan semua akun';
                if (current && profiles.some(p => p.name === current)) {
                    profileSelect.value = current;
                } else if (!profileSelect.value && profiles.length === 1) {
                    profileSelect.value = profiles[0].name;
                }
            };

            function renderPlatformBar() {
                platformBar.innerHTML = '';
                Object.entries(RISET_PLATFORM_UI).forEach(([key, cfg]) => {
                    const btn = document.createElement('button');
                    btn.type = 'button';
                    btn.className = 'riset-platform-btn' + (key === activeRisetPlatform ? ' active' : '');
                    btn.innerHTML = `<i class="${cfg.icon}"></i> ${cfg.label}`;
                    btn.onclick = () => {
                        activeRisetPlatform = key;
                        renderPlatformBar();
                        syncRisetUI();
                        populateRisetProfiles();
                        resultsGrid.innerHTML = '';
                    };
                    platformBar.appendChild(btn);
                });
            }

            function syncRisetUI() {
                const cfg = RISET_PLATFORM_UI[activeRisetPlatform] || RISET_PLATFORM_UI.shopee;
                titleEl.innerHTML = `<i class="${cfg.icon}"></i> ${cfg.title}`;
                hintEl.textContent = cfg.hint;
                keywordInput.placeholder = cfg.placeholder;
                loadingText.textContent = cfg.loading;
                noteEl.textContent = cfg.note;
                actionEl.textContent = cfg.action;
                badgeEl.innerHTML = cfg.supportsRegion
                    ? '<i class="fa-solid fa-user-check"></i> Riset via akun login + regional'
                    : '<i class="fa-solid fa-user-check"></i> Riset via akun login';
                regionWrap.classList.toggle('view-hidden', !cfg.supportsRegion);
                btnSearch.innerHTML = `<i class="fa-solid fa-search"></i> ${cfg.button}`;
            }

            function openRisetRoute(item) {
                const profile = profileSelect.value;
                if (!profile) {
                    showToast('Pilih profile yang sudah login dulu', 'error');
                    return;
                }
                api('/api/riset/open', 'POST', {
                    profile,
                    url: item.url,
                    label: item.title || item.action_label || 'Riset'
                }).then(res => {
                    if (res.success) {
                        showToast('Membuka jalur riset lewat akun terpilih...', 'success');
                    } else {
                        showToast(res.error || 'Gagal membuka jalur riset', 'error');
                    }
                }).catch(() => showToast('Gagal terhubung ke server', 'error'));
            }

            function renderDiscoveryLinks(items) {
                if (!items.length) {
                    resultsGrid.innerHTML = `<div class="riset-empty">${(RISET_PLATFORM_UI[activeRisetPlatform] || {}).empty || 'Belum ada jalur riset.'}</div>`;
                    return;
                }
                items.forEach(item => {
                    const cfg = RISET_PLATFORM_UI[activeRisetPlatform] || RISET_PLATFORM_UI.shopee;
                    const card = document.createElement('div');
                    card.className = 'riset-card riset-link-card';
                    card.innerHTML = `
                        <div class="riset-thumb-icon"><i class="${cfg.icon}"></i></div>
                        <div class="riset-title" title="${escapeHtml(item.title)}">${escapeHtml(item.title)}</div>
                        <div class="riset-card-subtitle">${escapeHtml(item.subtitle || '')}</div>
                        <div class="riset-actions">
                            <button type="button" class="btn-primary btn-open-route"><i class="fa-solid fa-arrow-up-right-from-square"></i> ${escapeHtml(item.action_label || 'Buka')}</button>
                        </div>
                    `;
                    card.querySelector('.btn-open-route').onclick = () => openRisetRoute(item);
                    resultsGrid.appendChild(card);
                });
            }

            function runRisetSearch() {
                const profile = profileSelect.value;
                const keyword = keywordInput.value.trim();
                if (!profile) {
                    showToast('Pilih profile yang sudah login dulu', 'error');
                    return;
                }
                if (!keyword) {
                    showToast('Keyword pencarian wajib diisi', 'error');
                    return;
                }
                loadingPanel.classList.remove('view-hidden');
                resultsGrid.innerHTML = '';
                api('/api/riset/search', 'POST', {
                    platform: activeRisetPlatform,
                    profile: profile,
                    keyword: keyword,
                    region: regionSelect.value,
                    limit: 20
                })
                    .then(res => {
                        loadingPanel.classList.add('view-hidden');
                        if (!res.success) {
                            showToast(res.error || 'Gagal menjalankan riset', 'error');
                            return;
                        }
                        renderDiscoveryLinks(res.results || []);
                    })
                    .catch(err => {
                        loadingPanel.classList.add('view-hidden');
                        showToast(err.message || 'Gagal menjalankan riset', 'error');
                    });
            }

            btnSearch.onclick = runRisetSearch;
            keywordInput.addEventListener('keydown', (e) => {
                if (e.key === 'Enter') runRisetSearch();
            });

            populateRisetProfiles();
            renderPlatformBar();
            syncRisetUI();
        }
        // ---------- Downloader View ----------
        // ---------- Repurpose (port inti LUMINA) ----------
        let repurposeInitialized = false;
        let rpPollTimer = null;
        let rpLastOutputDir = '';
        let rpLastVoFile = '';
        const rpModes = { capture: 'url' };
        let rpSplitFiles = [];
        let rpSplitSelected = new Set();

        function setRepurposeAction(action) {
            document.querySelectorAll('#rpActionNav .sub-tab-btn').forEach(btn => {
                btn.classList.toggle('active', btn.dataset.action === action);
            });
            document.querySelectorAll('#view-repurpose .repurpose-panel').forEach(panel => {
                panel.classList.toggle('active', panel.dataset.panel === action);
            });
        }

        function renderSplitFiles() {
            const wrap = document.getElementById('rpSplitFileList');
            const count = document.getElementById('rpSplitCount');
            if (!wrap || !count) return;
            count.textContent = `${rpSplitFiles.length} video`;
            count.style.color = rpSplitFiles.length ? 'var(--accent-color)' : 'var(--text-secondary)';
            if (!rpSplitFiles.length) {
                wrap.innerHTML = '<div class="rp-file-empty">Belum ada video. Tambahkan file, folder, atau URL untuk mulai split.</div>';
                return;
            }
            wrap.innerHTML = rpSplitFiles.map((path, idx) => {
                const isUrl = /^https?:\/\//i.test(path);
                const name = isUrl ? path : path.split(/[\\/]/).pop();
                const active = rpSplitSelected.has(path) ? ' active' : '';
                return `
                    <div class="rp-file-item${active}" data-path="${escapeHtml(path)}" data-idx="${idx}">
                        <div class="rp-file-icon"><i class="fa-solid ${isUrl ? 'fa-link' : 'fa-file-video'}"></i></div>
                        <div class="rp-file-main">
                            <div class="rp-file-name">${escapeHtml(name)}</div>
                            <div class="rp-file-path">${escapeHtml(path)}</div>
                        </div>
                    </div>`;
            }).join('');
            wrap.querySelectorAll('.rp-file-item').forEach(item => {
                item.onclick = () => {
                    const path = item.dataset.path;
                    if (rpSplitSelected.has(path)) rpSplitSelected.delete(path);
                    else rpSplitSelected.add(path);
                    renderSplitFiles();
                };
            });
        }

        function addSplitFiles(paths) {
            let changed = false;
            (paths || []).forEach(path => {
                const clean = String(path || '').trim();
                if (clean && !rpSplitFiles.includes(clean)) {
                    rpSplitFiles.push(clean);
                    changed = true;
                }
            });
            if (changed) renderSplitFiles();
        }

        // Panel progress sering ada di bagian bawah form panjang - kalau cuma
        // dihapus class view-hidden-nya tanpa di-scroll, user yang layar/
        // window-nya pas-pasan (mis. tinggi default GAMA 820px) tidak pernah
        // benar-benar MELIHAT progress bar-nya walau secara teknis "muncul".
        // Dipakai di SEMUA panel progress di seluruh app, bukan cuma Repurpose.
        function revealProgressPanel(el) {
            if (typeof el === 'string') el = document.getElementById(el);
            if (!el) return;
            el.classList.remove('view-hidden');
            requestAnimationFrame(() => {
                try { el.scrollIntoView({ behavior: 'smooth', block: 'nearest' }); } catch (e) {}
            });
        }

        function resetRepurposeProgress(label) {
            const panel = document.getElementById('rpProgress');
            revealProgressPanel(panel);
            document.getElementById('rpProgLbl').textContent = label || 'Memulai...';
            document.getElementById('rpProgPct').textContent = '0%';
            document.getElementById('rpProgBar').style.width = '0%';
            document.getElementById('rpProgMsg').textContent = '';
        }

        function startRepurposeJob(payload, triggerBtn, successLabel) {
            triggerBtn.disabled = true;
            resetRepurposeProgress('Memulai...');
            api('/api/repurpose/run', 'POST', payload).then(d => {
                if (!d.success) {
                    triggerBtn.disabled = false;
                    showToast(d.error || 'Gagal memulai', 'error');
                    return;
                }
                if (rpPollTimer) clearTimeout(rpPollTimer);
                const poll = () => {
                    fetch(`/api/youtube/job/${d.job_id}`).then(r => r.json()).then(s => {
                        const pct = s.pct || 0;
                        document.getElementById('rpProgPct').textContent = pct + '%';
                        document.getElementById('rpProgBar').style.width = pct + '%';
                        document.getElementById('rpProgLbl').textContent = s.message || 'Memproses...';
                        document.getElementById('rpProgMsg').textContent = s.error || '';
                        if (s.done) {
                            triggerBtn.disabled = false;
                            if (s.out_dir) rpLastOutputDir = s.out_dir;
                            if (s.success) showToast(successLabel || `Selesai: ${s.clips || 0} klip`, 'success');
                            else showToast(s.error || 'Repurpose gagal', 'error');
                            return;
                        }
                        rpPollTimer = setTimeout(poll, 1500);
                    }).catch(() => {});
                };
                poll();
            }).catch(() => {
                triggerBtn.disabled = false;
                showToast('Gagal terhubung ke server', 'error');
            });
        }

        function applyFaceTrackerStatus(status) {
            const note = document.getElementById('rpFaceTrackerNote');
            const check = document.getElementById('rpFaceTracker');
            if (!note || !check) return;
            if (status.face_tracker_available) {
                check.disabled = false;
                note.textContent = 'Face Tracker aktif akan mengabaikan segmen yang tidak menemukan wajah.';
            } else {
                check.checked = false;
                check.disabled = true;
                note.textContent = status.face_tracker_reason || 'Face Tracker belum tersedia di environment GAMA ini.';
            }
        }

        function syncSplitModeUI() {
            const mode = document.getElementById('rpSplitMode').value;
            document.getElementById('rpSplitCountWrap').classList.toggle('view-hidden', mode !== 'count');
            document.getElementById('rpSplitDurationWrap').classList.toggle('view-hidden', mode !== 'fixed');
        }

        function bindCaptureSourceToggle(groupId, modeKey, labelId, inputId, browseId) {
            document.querySelectorAll(`#${groupId} .scrape-mode-btn`).forEach(btn => {
                btn.onclick = () => {
                    document.querySelectorAll(`#${groupId} .scrape-mode-btn`).forEach(b => b.classList.remove('active'));
                    btn.classList.add('active');
                    rpModes[modeKey] = btn.dataset.mode;
                    const isUrl = rpModes[modeKey] === 'url';
                    document.getElementById(labelId).textContent = isUrl ? 'URL video (YouTube/TikTok/dll)' : 'Path file video lokal';
                    document.getElementById(inputId).placeholder = isUrl ? 'https://youtube.com/watch?v=...' : 'D:/folder/video.mp4';
                    document.getElementById(browseId).style.display = isUrl ? 'none' : '';
                };
            });
        }

        async function pickSplitVideos() {
            if (window.pywebview && window.pywebview.api && window.pywebview.api.pick_files) {
                try {
                    const files = await window.pywebview.api.pick_files('video');
                    addSplitFiles(files || []);
                    return;
                } catch (e) {
                    showToast('Gagal membuka dialog video', 'error');
                    return;
                }
            }
            showToast('Tambahkan video dari mode desktop GAMA agar path file lokal bisa dipakai.', 'info');
        }

        async function pickSplitFolder() {
            if (window.pywebview && window.pywebview.api && window.pywebview.api.pick_folder) {
                try {
                    const folder = await window.pywebview.api.pick_folder();
                    if (!folder) return;
                    const data = await api('/api/repurpose/list_videos', 'POST', { folder });
                    if (!data.success) {
                        showToast(data.error || 'Folder tidak bisa dibaca', 'error');
                        return;
                    }
                    addSplitFiles(data.files || []);
                    if (!(data.files || []).length) showToast('Tidak ada video yang ditemukan di folder itu', 'info');
                    return;
                } catch (e) {
                    showToast('Gagal membaca folder video', 'error');
                    return;
                }
            }
            showToast('Pilih folder video tersedia di mode desktop GAMA.', 'info');
        }

        function initRepurposeView() {
            fetch('/api/repurpose/status').then(r => r.json()).then(status => {
                const box = document.getElementById('repurposeUnavailable');
                if (!status.available) {
                    box.textContent = status.reason || 'ffmpeg belum siap.';
                    box.classList.remove('view-hidden');
                } else {
                    box.classList.add('view-hidden');
                }
                applyFaceTrackerStatus(status);
            }).catch(() => {});

            if (repurposeInitialized) return;
            repurposeInitialized = true;

            document.querySelectorAll('#rpActionNav .sub-tab-btn').forEach(btn => {
                btn.onclick = () => setRepurposeAction(btn.dataset.action);
            });
            setRepurposeAction('split');
            renderSplitFiles();

            bindCaptureSourceToggle('rpCaptureSourceMode', 'capture', 'rpCaptureSourceLabel', 'rpCaptureSource', 'rpCaptureBrowse');

            document.getElementById('rpSplitMode').onchange = syncSplitModeUI;
            syncSplitModeUI();

            document.getElementById('rpAddVideos').onclick = pickSplitVideos;
            document.getElementById('rpAddFolder').onclick = pickSplitFolder;
            const rpUrlRow = document.getElementById('rpUrlRow');
            const addSplitUrl = () => {
                const inp = document.getElementById('rpUrlInput');
                const url = inp.value.trim();
                if (!url) return;
                if (!/^https?:\/\//i.test(url)) {
                    showToast('URL harus diawali http:// atau https://', 'error');
                    return;
                }
                addSplitFiles([url]);
                inp.value = '';
                inp.focus();
            };
            document.getElementById('rpAddUrl').onclick = () => {
                rpUrlRow.classList.toggle('view-hidden');
                if (!rpUrlRow.classList.contains('view-hidden')) document.getElementById('rpUrlInput').focus();
            };
            document.getElementById('rpUrlAdd').onclick = addSplitUrl;
            document.getElementById('rpUrlInput').addEventListener('keydown', (e) => {
                if (e.key === 'Enter') { e.preventDefault(); addSplitUrl(); }
            });
            document.getElementById('rpRemoveVideos').onclick = () => {
                if (!rpSplitSelected.size) {
                    showToast('Pilih video yang mau dihapus dulu', 'info');
                    return;
                }
                rpSplitFiles = rpSplitFiles.filter(path => !rpSplitSelected.has(path));
                rpSplitSelected.clear();
                renderSplitFiles();
            };
            document.getElementById('rpClearVideos').onclick = () => {
                rpSplitFiles = [];
                rpSplitSelected.clear();
                renderSplitFiles();
            };
            document.getElementById('rpBrowseOutput').onclick = async () => {
                if (window.pywebview && window.pywebview.api && window.pywebview.api.pick_folder) {
                    try {
                        const folder = await window.pywebview.api.pick_folder();
                        if (folder) document.getElementById('rpOutputDir').value = folder;
                        return;
                    } catch (e) {
                        showToast('Gagal membuka dialog folder', 'error');
                        return;
                    }
                }
                document.getElementById('rpOutputDir').focus();
                showToast('Isi path folder output secara manual di mode browser', 'info');
            };

            document.getElementById('rpOpenFolder').onclick = () => {
                const path = document.getElementById('rpOutputDir').value.trim() || rpLastOutputDir;
                api('/api/repurpose/open_folder', 'POST', { path }).then(d => {
                    if (!d.success) showToast(d.error || 'Gagal membuka folder', 'error');
                }).catch(() => showToast('Gagal terhubung ke server', 'error'));
            };
            document.getElementById('rpCaptureOpenFolder').onclick = () => {
                const path = document.getElementById('rpOutputDir').value.trim() || rpLastOutputDir;
                api('/api/repurpose/open_folder', 'POST', { path }).then(d => {
                    if (!d.success) showToast(d.error || 'Gagal membuka folder', 'error');
                }).catch(() => showToast('Gagal terhubung ke server', 'error'));
            };

            document.getElementById('rpStartBtn').onclick = () => {
                if (!rpSplitFiles.length) {
                    showToast('Tambahkan video dulu sebelum mulai split', 'error');
                    return;
                }
                const splitMode = document.getElementById('rpSplitMode').value;
                const payload = {
                    sources: rpSplitFiles,
                    ratio: document.getElementById('rpRatio').value,
                    split_mode: splitMode,
                    seg_count: parseInt(document.getElementById('rpSplitCountValue').value, 10) || 5,
                    seg_duration: parseInt(document.getElementById('rpSplitDurationValue').value, 10) || 60,
                    face_tracker: document.getElementById('rpFaceTracker').checked,
                    turbo: document.getElementById('rpTurbo').checked,
                    output_dir: document.getElementById('rpOutputDir').value.trim(),
                    frames: false,
                };
                startRepurposeJob(payload, document.getElementById('rpStartBtn'), 'Split video selesai');
            };

            // -- Port LUMINA: helper job generik + picker folder --
            const rpJobAt = (url, payload, btn, label, onState) => {
                btn.disabled = true;
                resetRepurposeProgress('Memulai...');
                api(url, 'POST', payload).then(d => {
                    if (!d.success) {
                        btn.disabled = false;
                        showToast(d.error || 'Gagal memulai', 'error');
                        return;
                    }
                    if (rpPollTimer) clearTimeout(rpPollTimer);
                    const poll = () => {
                        fetch(`/api/youtube/job/${d.job_id}`).then(r => r.json()).then(s => {
                            const pct = s.pct || 0;
                            document.getElementById('rpProgPct').textContent = pct + '%';
                            document.getElementById('rpProgBar').style.width = pct + '%';
                            document.getElementById('rpProgLbl').textContent = s.message || 'Memproses...';
                            document.getElementById('rpProgMsg').textContent = s.error || '';
                            if (s.done) {
                                btn.disabled = false;
                                if (s.out_dir) rpLastOutputDir = s.out_dir;
                                if (onState) onState(s);
                                if (s.success) showToast(label || 'Selesai', 'success');
                                else showToast(s.error || 'Proses gagal', 'error');
                                return;
                            }
                            rpPollTimer = setTimeout(poll, 1500);
                        }).catch(() => {});
                    };
                    poll();
                }).catch(() => {
                    btn.disabled = false;
                    showToast('Gagal terhubung ke server', 'error');
                });
            };
            const openFolderOf = (inputId) => () => {
                const path = document.getElementById(inputId).value.trim() || rpLastOutputDir;
                api('/api/repurpose/open_folder', 'POST', { path }).then(d => {
                    if (!d.success) showToast(d.error || 'Gagal membuka folder', 'error');
                }).catch(() => showToast('Gagal terhubung ke server', 'error'));
            };
            document.querySelectorAll('.rp-pick-folder').forEach(btn => {
                btn.onclick = async () => {
                    if (!(window.pywebview && window.pywebview.api && window.pywebview.api.pick_folder)) {
                        showToast('Ketik path folder manual di mode browser', 'info');
                        return;
                    }
                    try {
                        const folder = await window.pywebview.api.pick_folder();
                        if (folder) document.getElementById(btn.dataset.target).value = folder;
                    } catch (e) { showToast('Gagal membuka dialog folder', 'error'); }
                };
            });
            // -- Capture Image (Lumina-style smart capture) --
            document.getElementById('rpCaptureOutBrowse').onclick = async () => {
                if (window.pywebview && window.pywebview.api && window.pywebview.api.pick_folder) {
                    try {
                        const folder = await window.pywebview.api.pick_folder();
                        if (folder) document.getElementById('rpCaptureOutDir').value = folder;
                    } catch (e) {}
                }
            };
            document.getElementById('rpCaptureBtn').onclick = () => {
                const source = document.getElementById('rpCaptureSource').value.trim();
                if (!source) {
                    showToast('Isi URL atau path file dulu', 'error');
                    return;
                }
                rpJobAt('/api/repurpose/smart_capture', {
                    source,
                    is_url: rpModes.capture === 'url',
                    interval: parseInt(document.getElementById('rpCaptureInterval').value, 10) || 10,
                    ratio: document.getElementById('rpCaptureRatio').value,
                    face_tracker: document.getElementById('rpCaptureFaceTracker').checked,
                    output_dir: document.getElementById('rpCaptureOutDir').value.trim(),
                }, document.getElementById('rpCaptureBtn'), 'Capture image selesai');
            };

            // -- Image to Video (cinematic) --
            let rpI2vImages = [];
            const renderI2vList = () => {
                const wrap = document.getElementById('rpI2vList');
                document.getElementById('rpI2vCount').textContent = `${rpI2vImages.length} gambar`;
                if (!rpI2vImages.length) {
                    wrap.innerHTML = '<div class="rp-file-empty">Belum ada gambar. Klik Browse Images (jpg/png/webp).</div>';
                    return;
                }
                wrap.innerHTML = rpI2vImages.map(p => `
                    <div class="rp-file-item">
                        <div class="rp-file-icon"><i class="fa-solid fa-image"></i></div>
                        <div class="rp-file-main">
                            <div class="rp-file-name">${escapeHtml(p.split(/[\\/]/).pop())}</div>
                            <div class="rp-file-path">${escapeHtml(p)}</div>
                        </div>
                    </div>`).join('');
            };
            renderI2vList();
            fetch('/api/repurpose/effects').then(r => r.json()).then(d => {
                const sel = document.getElementById('rpI2vEffect');
                sel.innerHTML = '<option value="Random"> Random (efek acak per gambar)</option>'
                    + (d.effects || ['Zoom In']).map(e => `<option value="${e}">${e}</option>`).join('');
            }).catch(() => {});
            document.getElementById('rpI2vAdd').onclick = async () => {
                if (!(window.pywebview && window.pywebview.api && window.pywebview.api.pick_files)) {
                    showToast('Pilih gambar tersedia di mode desktop GAMA', 'info');
                    return;
                }
                try {
                    const files = await window.pywebview.api.pick_files('image');
                    (files || []).forEach(f => { if (f && !rpI2vImages.includes(f)) rpI2vImages.push(f); });
                    renderI2vList();
                } catch (e) { showToast('Gagal membuka dialog gambar', 'error'); }
            };
            document.getElementById('rpI2vClear').onclick = () => { rpI2vImages = []; renderI2vList(); };
            document.getElementById('rpI2vOutBrowse').onclick = async () => {
                if (window.pywebview && window.pywebview.api && window.pywebview.api.pick_folder) {
                    try {
                        const folder = await window.pywebview.api.pick_folder();
                        if (folder) document.getElementById('rpI2vOutDir').value = folder;
                    } catch (e) {}
                }
            };
            document.getElementById('rpI2vBtn').onclick = () => {
                if (!rpI2vImages.length) {
                    showToast('Pilih minimal 1 gambar dulu', 'error');
                    return;
                }
                rpJobAt('/api/repurpose/img2video', {
                    images: rpI2vImages,
                    effect: document.getElementById('rpI2vEffect').value,
                    duration: parseInt(document.getElementById('rpI2vDuration').value, 10) || 5,
                    ratio: document.getElementById('rpI2vRatio').value,
                    output_dir: document.getElementById('rpI2vOutDir').value.trim(),
                }, document.getElementById('rpI2vBtn'), 'Cinematic clips selesai');
            };
            document.getElementById('rpI2vOpenFolder').onclick = openFolderOf('rpI2vOutDir');

            // -- Transkrip & Rewrite --
            document.getElementById('rpTrBtn').onclick = () => {
                const source = document.getElementById('rpTrSource').value.trim();
                if (!source) {
                    showToast('Pilih file video/audio dulu', 'error');
                    return;
                }
                rpJobAt('/api/repurpose/transcribe', { source },
                    document.getElementById('rpTrBtn'), 'Transkrip selesai', (s) => {
                        if (s.success && s.text) document.getElementById('rpTrEditor').value = s.text;
                    });
            };
            document.getElementById('rpTrRewriteBtn').onclick = () => {
                const text = document.getElementById('rpTrEditor').value.trim();
                if (!text) {
                    showToast('Editor masih kosong', 'error');
                    return;
                }
                const mins = (parseInt(document.getElementById('rpTrMin').value, 10) || 0)
                    + (parseInt(document.getElementById('rpTrSec').value, 10) || 0) / 60;
                if (mins <= 0) {
                    showToast('Isi target durasi dulu', 'error');
                    return;
                }
                const btn = document.getElementById('rpTrRewriteBtn');
                btn.disabled = true;
                showToast('Gemini menulis ulang naskah...', 'info');
                api('/api/repurpose/rewrite', 'POST', {
                    text,
                    style: document.getElementById('rpTrStyle').value,
                    duration_minutes: mins,
                }).then(d => {
                    btn.disabled = false;
                    if (d.success && d.text) {
                        document.getElementById('rpTrEditor').value = d.text;
                        showToast('Naskah selesai di-rewrite', 'success');
                    } else showToast(d.error || 'Rewrite gagal', 'error');
                }).catch(() => { btn.disabled = false; showToast('Gagal terhubung ke server', 'error'); });
            };
            document.getElementById('rpTrCopyBtn').onclick = async () => {
                const text = document.getElementById('rpTrEditor').value;
                if (!text.trim()) return showToast('Editor masih kosong', 'info');
                if (window.pywebview && window.pywebview.api && window.pywebview.api.clipboard_set) {
                    try { await window.pywebview.api.clipboard_set(text); } catch (e) {}
                } else {
                    try { await navigator.clipboard.writeText(text); } catch (e) {}
                }
                showToast('Teks disalin ke clipboard', 'success');
            };
            document.getElementById('rpTrToVoBtn').onclick = () => {
                const text = document.getElementById('rpTrEditor').value.trim();
                if (!text) return showToast('Editor masih kosong', 'info');
                document.getElementById('rpVoScript').value = text;
                setRepurposeAction('voice-over');
                showToast('Naskah dikirim ke Voice Over', 'success');
            };

            // -- Voice Over via Gemini Canvas (otomatis penuh) --
            fillRpVoAccounts();

            document.getElementById('rpVoGenerateBtn').onclick = () => {
                const script = document.getElementById('rpVoScript').value.trim();
                if (!script) { showToast('Tulis naskah voice over dulu', 'error'); return; }
                const email = document.getElementById('rpVoAccount').value;
                if (!email) { showToast('Pilih akun Gmail dulu', 'error'); return; }
                const payload = {
                    email,
                    script,
                    voice: document.getElementById('rpVoVoice').value,
                    bahasa: document.getElementById('rpVoBahasa').value,
                    gaya: document.getElementById('rpVoLang').value.trim(),
                    out_dir: document.getElementById('rpVoOutDir').value.trim(),
                };
                rpJobAt('/api/repurpose/vo_generate', payload, document.getElementById('rpVoGenerateBtn'),
                    'Voice over selesai', s => {
                        if (s.success && s.files && s.files.length) {
                            rpLastOutputDir = s.folder || rpLastOutputDir;
                            rpLastVoFile = s.files[0] || rpLastVoFile;
                        }
                    });
            };
            document.getElementById('rpVoOpenFolderBtn').onclick = openFolderOf('rpVoOutDir');
            document.getElementById('rpVoToMixerBtn').onclick = () => {
                if (!rpLastVoFile) { showToast('Belum ada hasil VO. Generate dulu.', 'error'); return; }
                if (!rpMixVos.includes(rpLastVoFile)) rpMixVos.push(rpLastVoFile);
                renderMixVos();
                const urutanBtn = document.querySelector('#rpMixModeToggle [data-mode="urutan"]');
                if (urutanBtn && !urutanBtn.classList.contains('active')) urutanBtn.click();
                setRepurposeAction('mixer');
                showToast('VO dikirim ke Video Mixer', 'success');
            };

            // -- Video Mixer --
            let rpMixMode = 'acak';
            let rpMixAudios = [];
            let rpMixVideos = [];
            let rpMixVos = [];
            document.querySelectorAll('#rpMixModeToggle .scrape-mode-btn').forEach(btn => {
                btn.onclick = () => {
                    document.querySelectorAll('#rpMixModeToggle .scrape-mode-btn').forEach(b => b.classList.remove('active'));
                    btn.classList.add('active');
                    rpMixMode = btn.dataset.mode;
                    document.getElementById('rpMixFolderWrap').classList.toggle('view-hidden', rpMixMode !== 'acak');
                    document.getElementById('rpMixGabungWrap').classList.toggle('view-hidden', rpMixMode !== 'gabung');
                    document.getElementById('rpMixUrutanWrap').classList.toggle('view-hidden', rpMixMode !== 'urutan');
                    // Mode berurutan pakai daftar VO sendiri (bukan daftar
                    // audio acak generik punya mode Acak Urutan).
                    document.getElementById('rpMixAudioWrap').classList.toggle('view-hidden', rpMixMode === 'urutan');
                };
            });

            const renderMixVideos = () => {
                const wrap = document.getElementById('rpMixVideoList');
                document.getElementById('rpMixVideoCount').textContent = `${rpMixVideos.length} video`;
                if (!rpMixVideos.length) {
                    wrap.innerHTML = '<div class="rp-file-empty">Belum ada video. Tambahkan video 1, 2, dst sesuai urutan gabungnya.</div>';
                    return;
                }
                wrap.innerHTML = rpMixVideos.map((p, i) => `
                    <div class="rp-file-item">
                        <div class="rp-file-icon">${i + 1}</div>
                        <div class="rp-file-main">
                            <div class="rp-file-name">${escapeHtml(p.split(/[\\/]/).pop())}</div>
                            <div class="rp-file-path">${escapeHtml(p)}</div>
                        </div>
                        <div class="rp-toolbar-actions">
                            <button type="button" class="btn-secondary neutral-lite mix-video-up" data-idx="${i}" ${i === 0 ? 'disabled' : ''} title="Naik"><i class="fa-solid fa-arrow-up"></i></button>
                            <button type="button" class="btn-secondary neutral-lite mix-video-down" data-idx="${i}" ${i === rpMixVideos.length - 1 ? 'disabled' : ''} title="Turun"><i class="fa-solid fa-arrow-down"></i></button>
                            <button type="button" class="btn-secondary mix-video-remove" data-idx="${i}" title="Hapus"><i class="fa-solid fa-xmark"></i></button>
                        </div>
                    </div>`).join('');
                wrap.querySelectorAll('.mix-video-remove').forEach(btn => {
                    btn.onclick = () => { rpMixVideos.splice(parseInt(btn.dataset.idx, 10), 1); renderMixVideos(); };
                });
                wrap.querySelectorAll('.mix-video-up').forEach(btn => {
                    btn.onclick = () => {
                        const i = parseInt(btn.dataset.idx, 10);
                        if (i > 0) { [rpMixVideos[i - 1], rpMixVideos[i]] = [rpMixVideos[i], rpMixVideos[i - 1]]; renderMixVideos(); }
                    };
                });
                wrap.querySelectorAll('.mix-video-down').forEach(btn => {
                    btn.onclick = () => {
                        const i = parseInt(btn.dataset.idx, 10);
                        if (i < rpMixVideos.length - 1) { [rpMixVideos[i + 1], rpMixVideos[i]] = [rpMixVideos[i], rpMixVideos[i + 1]]; renderMixVideos(); }
                    };
                });
            };
            renderMixVideos();

            window.gamaSendVideosToMixer = function(paths) {
                const videos = (paths || [])
                    .map(p => String(p || '').trim())
                    .filter(p => p && /\.(mp4|mov|mkv|webm)$/i.test(p));
                if (!videos.length) return false;
                let added = 0;
                videos.forEach(path => {
                    if (!rpMixVideos.includes(path)) {
                        rpMixVideos.push(path);
                        added += 1;
                    }
                });
                renderMixVideos();
                switchView('repurpose');
                const urutanBtn = document.querySelector('#rpMixModeToggle [data-mode="urutan"]');
                if (urutanBtn && !urutanBtn.classList.contains('active')) urutanBtn.click();
                const genPlatform = document.getElementById('genApPlatform');
                const genStyle = document.getElementById('genApStyle');
                const genCta = document.getElementById('genCtaType');
                const storyName = document.getElementById('genStoryboardName');
                if (genPlatform && document.getElementById('rpMixCaptionPlatform')) {
                    const p = genPlatform.value === 'shopee' ? 'shopee' : (genPlatform.value || 'tiktok');
                    document.getElementById('rpMixCaptionPlatform').value = p;
                }
                if (genStyle && document.getElementById('rpMixCaptionType')) {
                    let t = 'auto';
                    if (genStyle.value === 'tiktok_go_wisata') t = 'wisata';
                    else if (genStyle.value === 'tiktok_go_kuliner') t = 'kuliner';
                    else if (genStyle.value === 'tiktok_go_hotel') t = 'hotel';
                    else if ((genCta && genCta.value === 'shopee_aff') || genPlatform?.value === 'shopee') t = 'affiliate';
                    else if (genCta && genCta.value === 'tiktok_go') t = 'tiktok_go';
                    document.getElementById('rpMixCaptionType').value = t;
                }
                if (storyName && storyName.value.trim() && document.getElementById('rpMixCaptionExtra')) {
                    document.getElementById('rpMixCaptionExtra').value = storyName.value.trim();
                }
                setRepurposeAction('mixer');
                showToast(`${added || videos.length} video dikirim ke Video Mixer. Bisa langsung digabung tanpa VO.`, 'success');
                return true;
            };

            document.getElementById('rpMixVideoAdd').onclick = async () => {
                if (!(window.pywebview && window.pywebview.api && window.pywebview.api.pick_files)) {
                    showToast('Pilih video tersedia di mode desktop GAMA', 'info');
                    return;
                }
                try {
                    const files = await window.pywebview.api.pick_files('video');
                    (files || []).forEach(f => { if (f) rpMixVideos.push(f); });
                    renderMixVideos();
                } catch (e) { showToast('Gagal membuka dialog video', 'error'); }
            };
            document.getElementById('rpMixVideoClear').onclick = () => { rpMixVideos = []; renderMixVideos(); };
            const renderMixAudios = () => {
                const wrap = document.getElementById('rpMixAudioList');
                document.getElementById('rpMixAudioCount').textContent = `${rpMixAudios.length} audio`;
                if (!rpMixAudios.length) {
                    wrap.innerHTML = '<div class="rp-file-empty">Belum ada audio. Tanpa audio, output jadi video-only 60 detik.</div>';
                    return;
                }
                wrap.innerHTML = rpMixAudios.map(p => `
                    <div class="rp-file-item">
                        <div class="rp-file-icon"><i class="fa-solid fa-music"></i></div>
                        <div class="rp-file-main">
                            <div class="rp-file-name">${escapeHtml(p.split(/[\\/]/).pop())}</div>
                            <div class="rp-file-path">${escapeHtml(p)}</div>
                        </div>
                    </div>`).join('');
            };
            renderMixAudios();
            document.getElementById('rpMixAudioAdd').onclick = async () => {
                if (!(window.pywebview && window.pywebview.api && window.pywebview.api.pick_files)) {
                    showToast('Pilih audio tersedia di mode desktop GAMA', 'info');
                    return;
                }
                try {
                    const files = await window.pywebview.api.pick_files('audio');
                    (files || []).forEach(f => { if (f && !rpMixAudios.includes(f)) rpMixAudios.push(f); });
                    renderMixAudios();
                } catch (e) { showToast('Gagal membuka dialog audio', 'error'); }
            };
            document.getElementById('rpMixAudioClear').onclick = () => { rpMixAudios = []; renderMixAudios(); };
            const renderMixVos = () => {
                const wrap = document.getElementById('rpMixVoList');
                document.getElementById('rpMixVoCount').textContent = `${rpMixVos.length} VO`;
                if (!rpMixVos.length) {
                    wrap.innerHTML = '<div class="rp-file-empty">Belum ada VO. Mode ini tetap bisa digabung langsung tanpa voice over.</div>';
                    return;
                }
                wrap.innerHTML = rpMixVos.map((p, i) => `
                    <div class="rp-file-item">
                        <div class="rp-file-icon"><i class="fa-solid fa-microphone"></i></div>
                        <div class="rp-file-main">
                            <div class="rp-file-name">${escapeHtml(p.split(/[\\/]/).pop())}</div>
                            <div class="rp-file-path">${escapeHtml(p)}</div>
                        </div>
                        <div class="rp-toolbar-actions">
                            <button type="button" class="btn-secondary mix-vo-remove" data-idx="${i}" title="Hapus"><i class="fa-solid fa-xmark"></i></button>
                        </div>
                    </div>`).join('');
                wrap.querySelectorAll('.mix-vo-remove').forEach(btn => {
                    btn.onclick = () => { rpMixVos.splice(parseInt(btn.dataset.idx, 10), 1); renderMixVos(); };
                });
            };
            renderMixVos();
            document.getElementById('rpMixVoAdd').onclick = async () => {
                if (!(window.pywebview && window.pywebview.api && window.pywebview.api.pick_files)) {
                    showToast('Pilih VO tersedia di mode desktop GAMA', 'info');
                    return;
                }
                try {
                    const files = await window.pywebview.api.pick_files('audio');
                    (files || []).forEach(f => { if (f && !rpMixVos.includes(f)) rpMixVos.push(f); });
                    renderMixVos();
                } catch (e) { showToast('Gagal membuka dialog VO', 'error'); }
            };
            document.getElementById('rpMixVoClear').onclick = () => { rpMixVos = []; renderMixVos(); };
            document.getElementById('rpMixBtn').onclick = () => {
                const payload = {
                    mode: rpMixMode,
                    folder: document.getElementById('rpMixFolder').value.trim(),
                    folder1: document.getElementById('rpMixFolder1').value.trim(),
                    folder2: document.getElementById('rpMixFolder2').value.trim(),
                    count: Math.max(0, parseInt(document.getElementById('rpMixCount').value, 10) || 0),
                    ratio: document.getElementById('rpMixRatio').value,
                    resolution: document.getElementById('rpMixRes').value,
                    shuffle: true,
                    delete_used: document.getElementById('rpMixDelUsed').checked,
                    audios: rpMixAudios,
                    audio_vol: parseInt(document.getElementById('rpMixVol').value, 10) || 100,
                    audio_pitch: parseInt(document.getElementById('rpMixPitch').value, 10) || 0,
                    backsound_path: document.getElementById('rpMixBs').value.trim(),
                    backsound_vol: parseInt(document.getElementById('rpMixBsVol').value, 10) || 50,
                    wm_path: document.getElementById('rpMixWm').value.trim(),
                    wm_scale: (parseInt(document.getElementById('rpMixWmScale').value, 10) || 15) / 100,
                    wm_opacity: 1.0,
                    wm_x_pct: parseFloat(document.getElementById('rpMixWmPos').value.split(',')[0]),
                    wm_y_pct: parseFloat(document.getElementById('rpMixWmPos').value.split(',')[1]),
                    output_dir: document.getElementById('rpMixOutDir').value.trim(),
                    videos: rpMixVideos,
                    vo_files: rpMixVos,
                    generate_caption: document.getElementById('rpMixCaptionOn').checked,
                    caption_platform: document.getElementById('rpMixCaptionPlatform').value,
                    caption_content_type: document.getElementById('rpMixCaptionType').value,
                    caption_extra: document.getElementById('rpMixCaptionExtra').value.trim(),
                };
                if (payload.mode === 'gabung' && (!payload.folder1 || !payload.folder2)) {
                    showToast('Lengkapi Folder 1 dan Folder 2 dulu', 'error');
                    return;
                }
                if (payload.mode === 'acak' && !payload.folder) {
                    showToast('Pilih folder clip dulu', 'error');
                    return;
                }
                if (payload.mode === 'urutan') {
                    if (payload.videos.length < 1) {
                        showToast('Tambahkan minimal 1 video dulu', 'error');
                        return;
                    }
                    if (!payload.vo_files.length) payload.count = 1;
                }
                rpJobAt('/api/repurpose/mix', payload, document.getElementById('rpMixBtn'), 'Video mixer selesai');
            };
            document.getElementById('rpMixOpenFolder').onclick = openFolderOf('rpMixOutDir');
        }

        // ---------- Clipper (VideoClipperPro engine) ----------
        let clipperInitialized = false;
        let clSourceMode = 'url';
        let clPollTimer = null;
        let clClips = [];         // [{start, end, label, video, selected}] - video per klip utk multi-mode
        let clVideoPath = '';     // video default (mode URL tunggal & file)
        let clSrtPath = '';
        let clVideoDuration = 0;
        let clBatchUrls = [];     // URL list utk mode multi/username/keyword

        function _fmtDur(s) {
            s = Math.round(s);
            const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), sec = s % 60;
            return h ? `${h}j ${m}m ${sec}d` : (m ? `${m}m ${sec}d` : `${sec}d`);
        }

        function clRenderClips() {
            const list = document.getElementById('clClipList');
            const selCount = clClips.filter(c => c.selected).length;
            document.getElementById('clManagerInfo').textContent = `${selCount}/${clClips.length} klip terpilih`;
            if (!clClips.length) { list.innerHTML = '<p style="opacity:0.5;padding:0.5rem;">Belum ada klip.</p>'; return; }
            list.innerHTML = clClips.map((c, i) => `
                <div class="rp-file-item" style="align-items:center;gap:0.4rem;${c.selected ? '' : 'opacity:0.5;'}">
                    <input type="checkbox" class="cl-clip-cb" data-idx="${i}" ${c.selected ? 'checked' : ''} style="margin-right:0.3rem;cursor:pointer;">
                    <div class="rp-file-icon" style="font-size:0.75rem;min-width:1.6rem;text-align:center;"><span style="font-weight:600;">#${i + 1}</span></div>
                    <div class="rp-file-main" style="flex:1;display:flex;gap:0.4rem;flex-wrap:wrap;align-items:center;">
                        <input type="number" class="modal-input cl-clip-start" data-idx="${i}" value="${c.start}" step="0.1" min="0" style="width:5.5rem;font-size:0.82rem;" title="Detik mulai">
                        <span style="opacity:0.4;"></span>
                        <input type="number" class="modal-input cl-clip-end" data-idx="${i}" value="${c.end}" step="0.1" min="0" style="width:5.5rem;font-size:0.82rem;" title="Detik selesai">
                        <span style="font-size:0.75rem;opacity:0.5;">(${_fmtDur(c.end - c.start)})</span>
                        <input type="text" class="modal-input cl-clip-label" data-idx="${i}" value="${escapeHtml(c.label || '')}" style="flex:1;min-width:6rem;font-size:0.82rem;" placeholder="Label (opsional)">
                    </div>
                </div>`).join('');
            // Wire up edits
            list.querySelectorAll('.cl-clip-cb').forEach(cb => {
                cb.onchange = () => { clClips[+cb.dataset.idx].selected = cb.checked; clRenderClips(); };
            });
            list.querySelectorAll('.cl-clip-start').forEach(inp => {
                inp.onchange = () => { clClips[+inp.dataset.idx].start = parseFloat(inp.value) || 0; clRenderClips(); };
            });
            list.querySelectorAll('.cl-clip-end').forEach(inp => {
                inp.onchange = () => { clClips[+inp.dataset.idx].end = parseFloat(inp.value) || 0; clRenderClips(); };
            });
            list.querySelectorAll('.cl-clip-label').forEach(inp => {
                inp.onchange = () => { clClips[+inp.dataset.idx].label = inp.value; };
            });
        }

        function clShowProgress(label, pct) {
            const panel = document.getElementById('clProgress');
            revealProgressPanel(panel);
            document.getElementById('clProgLbl').textContent = label || 'Memproses...';
            document.getElementById('clProgPct').textContent = (pct || 0) + '%';
            document.getElementById('clProgBar').style.width = (pct || 0) + '%';
            document.getElementById('clProgMsg').textContent = '';
        }

        let clCurrentJobId = null;
        let clLastCsv = '';

        function clPollJob(jobId, onDone) {
            clCurrentJobId = jobId;
            if (clPollTimer) clearTimeout(clPollTimer);
            const poll = () => {
                fetch(`/api/youtube/job/${jobId}`).then(r => r.json()).then(s => {
                    const pct = s.pct || 0;
                    document.getElementById('clProgPct').textContent = pct + '%';
                    document.getElementById('clProgBar').style.width = pct + '%';
                    document.getElementById('clProgLbl').textContent = s.message || 'Memproses...';
                    document.getElementById('clProgMsg').textContent = s.error || '';
                    // Estimasi ukuran & waktu (dikirim backend di pesan pertama)
                    const estEl = document.getElementById('clProgEst');
                    if (estEl && (s.est_size_mb || s.est_time_min)) {
                        estEl.textContent = `Est. ~${s.est_size_mb || '?'} MB  -  ~${s.est_time_min || '?'} menit`;
                    }
                    if (s.done) {
                        clCurrentJobId = null;
                        onDone(s);
                        return;
                    }
                    clPollTimer = setTimeout(poll, 1200);
                }).catch(() => { clPollTimer = setTimeout(poll, 3000); });
            };
            poll();
        }

        function initClipperView() {
            fetch('/api/clipper/status').then(r => r.json()).then(s => {
                const box = document.getElementById('clipperUnavailable');
                if (!s.available) { box.textContent = s.reason || 'ffmpeg belum siap.'; box.classList.remove('view-hidden'); }
                else box.classList.add('view-hidden');
            }).catch(() => {});

            if (clipperInitialized) return;
            clipperInitialized = true;

            // -- Panel visibility per source mode --
            const clPanelIds = { url: 'clPanelUrl', multi: 'clPanelMulti', username: 'clPanelUsername', keyword: 'clPanelKeyword', file: 'clPanelFile' };
            function clSwitchPanel(mode) {
                clSourceMode = mode;
                Object.entries(clPanelIds).forEach(([m, id]) => {
                    const el = document.getElementById(id);
                    if (el) el.classList.toggle('view-hidden', m !== mode);
                });
                document.querySelectorAll('#clSourceMode .scrape-mode-btn').forEach(b =>
                    b.classList.toggle('active', b.dataset.mode === mode));
            }
            document.querySelectorAll('#clSourceMode .scrape-mode-btn').forEach(btn => {
                btn.onclick = () => clSwitchPanel(btn.dataset.mode);
            });

            // Open folder
            document.getElementById('clOpenFolder').onclick = () => {
                api('/api/clipper/open_folder', 'POST', {}).then(d => {
                    if (!d.success) showToast(d.error || 'Gagal membuka folder', 'error');
                }).catch(() => showToast('Gagal terhubung ke server', 'error'));
            };

            // -- Multi Link: textarea counter + paste + clear --
            const clMultiArea = document.getElementById('clMultiLinks');
            function clCountMultiUrls() {
                const urls = clMultiArea.value.split('\n').map(s => s.trim()).filter(s => s.startsWith('http'));
                document.getElementById('clMultiCount').textContent = `${urls.length} URL terdeteksi`;
                return urls;
            }
            clMultiArea.oninput = clCountMultiUrls;
            document.getElementById('clMultiPaste').onclick = async () => {
                try {
                    const text = await navigator.clipboard.readText();
                    clMultiArea.value = (clMultiArea.value ? clMultiArea.value + '\n' : '') + text;
                    clCountMultiUrls();
                } catch { showToast('Gagal akses clipboard', 'error'); }
            };
            document.getElementById('clMultiClear').onclick = () => { clMultiArea.value = ''; clCountMultiUrls(); };

            // -- By Username: Fetch button --
            document.getElementById('clFetchUsernameBtn').onclick = () => {
                const username = document.getElementById('clUsername').value.trim();
                if (!username) { showToast('Isi username dulu', 'error'); return; }
                const btn = document.getElementById('clFetchUsernameBtn');
                btn.disabled = true; btn.innerHTML = '<i class="fa-solid fa-spinner fa-spin"></i> Mengambil...';
                const payload = {
                    platform: document.getElementById('clUsernamePlatform').value,
                    username,
                    max_count: parseInt(document.getElementById('clUsernameMaxCount').value, 10) || 5,
                    sort: document.getElementById('clUsernameSort').value,
                };
                api('/api/clipper/fetch_by_username', 'POST', payload).then(d => {
                    btn.disabled = false; btn.innerHTML = '<i class="fa-solid fa-download"></i> Fetch Video List';
                    if (!d.success) { showToast(d.error || 'Gagal fetch', 'error'); return; }
                    clBatchUrls = d.urls || [];
                    document.getElementById('clUsernameResultText').textContent = `${d.count} video ditemukan dari @${username}`;
                    document.getElementById('clUsernameResult').classList.remove('view-hidden');
                    const listEl = document.getElementById('clUsernameUrlList');
                    listEl.style.display = 'block';
                    listEl.innerHTML = clBatchUrls.map((u, i) =>
                        `<div style="font-size:0.76rem;padding:0.2rem 0;border-bottom:1px solid rgba(255,255,255,0.05);overflow:hidden;text-overflow:ellipsis;white-space:nowrap;" title="${escapeHtml(u)}">`+
                        `<span style="opacity:0.5;margin-right:0.3rem;">${i+1}.</span><a href="${escapeHtml(u)}" target="_blank" style="color:var(--accent)">${escapeHtml(u)}</a></div>`
                    ).join('');
                    showToast(`${d.count} video siap dianalisa`, 'success');
                }).catch(() => { btn.disabled = false; btn.innerHTML = '<i class="fa-solid fa-download"></i> Fetch Video List'; showToast('Gagal fetch', 'error'); });
            };

            // -- By Keyword: Search button --
            document.getElementById('clFetchKeywordBtn').onclick = () => {
                const keyword = document.getElementById('clKeyword').value.trim();
                if (!keyword) { showToast('Isi keyword dulu', 'error'); return; }
                const btn = document.getElementById('clFetchKeywordBtn');
                btn.disabled = true; btn.innerHTML = '<i class="fa-solid fa-spinner fa-spin"></i> Mencari...';
                const payload = {
                    platform: document.getElementById('clKeywordPlatform').value,
                    keyword,
                    max_count: parseInt(document.getElementById('clKeywordMaxCount').value, 10) || 5,
                    sort: document.getElementById('clKeywordSort').value,
                };
                api('/api/clipper/fetch_by_keyword', 'POST', payload).then(d => {
                    btn.disabled = false; btn.innerHTML = '<i class="fa-solid fa-search"></i> Cari Video';
                    if (!d.success) { showToast(d.error || 'Gagal cari', 'error'); return; }
                    clBatchUrls = d.urls || [];
                    document.getElementById('clKeywordResultText').textContent = `${d.count} video ditemukan untuk "${keyword}"`;
                    document.getElementById('clKeywordResult').classList.remove('view-hidden');
                    const listEl = document.getElementById('clKeywordUrlList');
                    listEl.style.display = 'block';
                    listEl.innerHTML = clBatchUrls.map((u, i) =>
                        `<div style="font-size:0.76rem;padding:0.2rem 0;border-bottom:1px solid rgba(255,255,255,0.05);overflow:hidden;text-overflow:ellipsis;white-space:nowrap;" title="${escapeHtml(u)}">`+
                        `<span style="opacity:0.5;margin-right:0.3rem;">${i+1}.</span><a href="${escapeHtml(u)}" target="_blank" style="color:var(--accent)">${escapeHtml(u)}</a></div>`
                    ).join('');
                    showToast(`${d.count} video siap dianalisa`, 'success');
                }).catch(() => { btn.disabled = false; btn.innerHTML = '<i class="fa-solid fa-search"></i> Cari Video'; showToast('Gagal cari', 'error'); });
            };

            // -- FASE 1: Analisa --
            // Helper: analisa 1 URL/path dan push klip ke clClips[]
            function clAnalyzeOneUrl(url, isUrl, onClipsPushed, onError) {
                const payload = {
                    source: url,
                    is_url: isUrl,
                    mode: document.getElementById('clMode').value,
                    clip_duration: parseInt(document.getElementById('clDuration').value, 10) || 0,
                    clip_count: parseInt(document.getElementById('clCount').value, 10) || 0,
                    sub_lang: document.getElementById('clSubLang').value,
                    min_dur: parseInt(document.getElementById('clMinDur').value, 10) || 0,
                    max_dur: parseInt(document.getElementById('clMaxDur').value, 10) || 0,
                };
                return new Promise((resolve, reject) => {
                    api('/api/clipper/analyze', 'POST', payload).then(d => {
                        if (!d.success) { reject(d.error || 'Gagal'); return; }
                        clPollJob(d.job_id, s => {
                            if (s.success) {
                                const newClips = (s.clip_list || []).map(c => ({
                                    start: c.start || 0,
                                    end: c.end || 0,
                                    label: c.label || '',
                                    video: s.video_path || '',   // path unik per video (multi-mode)
                                    selected: true,
                                }));
                                clClips.push(...newClips);
                                // Simpan video pertama sebagai default fallback
                                if (!clVideoPath && s.video_path) {
                                    clVideoPath = s.video_path;
                                    clSrtPath = s.srt_path || '';
                                    clVideoDuration = s.video_duration || 0;
                                }
                                if (onClipsPushed) onClipsPushed(s, newClips.length);
                                resolve(s);
                            } else {
                                if (onError) onError(s.error || 'Analisa gagal');
                                reject(s.error || 'Analisa gagal');
                            }
                        });
                    }).catch(() => { reject('Gagal terhubung ke server'); });
                });
            }

            document.getElementById('clAnalyzeBtn').onclick = async () => {
                const btn = document.getElementById('clAnalyzeBtn');
                btn.disabled = true;
                clClips = [];       // reset
                clVideoPath = '';
                clSrtPath = '';
                clVideoDuration = 0;
                document.getElementById('clManager').classList.add('view-hidden');
                document.getElementById('clVideoInfo').classList.add('view-hidden');
                clShowProgress('Memulai analisa...', 0);

                // Tentukan daftar URL / source tergantung mode
                let urlList = [];
                let isUrlMode = true;

                if (clSourceMode === 'url') {
                    const src = document.getElementById('clSource').value.trim();
                    if (!src) { btn.disabled = false; showToast('Isi URL dulu', 'error'); document.getElementById('clProgress').classList.add('view-hidden'); return; }
                    urlList = [src]; isUrlMode = true;

                } else if (clSourceMode === 'file') {
                    const src = document.getElementById('clSourceFile').value.trim();
                    if (!src) { btn.disabled = false; showToast('Pilih file video dulu', 'error'); document.getElementById('clProgress').classList.add('view-hidden'); return; }
                    urlList = [src]; isUrlMode = false;

                } else if (clSourceMode === 'multi') {
                    urlList = document.getElementById('clMultiLinks').value.split('\n').map(s => s.trim()).filter(s => s.startsWith('http'));
                    if (!urlList.length) { btn.disabled = false; showToast('Tidak ada URL valid di daftar', 'error'); document.getElementById('clProgress').classList.add('view-hidden'); return; }
                    if (urlList.length > 20) { urlList = urlList.slice(0, 20); showToast('Dibatasi 20 URL pertama', 'info'); }
                    isUrlMode = true;

                } else if (clSourceMode === 'username' || clSourceMode === 'keyword') {
                    if (!clBatchUrls.length) { btn.disabled = false; showToast('Klik "Fetch Video List" / "Cari Video" dulu', 'error'); document.getElementById('clProgress').classList.add('view-hidden'); return; }
                    urlList = clBatchUrls; isUrlMode = true;
                }

                // Single URL - jalankan analisa normal
                if (urlList.length === 1) {
                    clAnalyzeOneUrl(urlList[0], isUrlMode, (s) => {
                        btn.disabled = false;
                        const infoPanel = document.getElementById('clVideoInfo');
                        infoPanel.classList.remove('view-hidden');
                        document.getElementById('clInfoDur').textContent = _fmtDur(clVideoDuration);
                        document.getElementById('clInfoFile').textContent = clVideoPath.split(/[\\/]/).pop() || '';
                        document.getElementById('clInfoSubs').textContent = s.has_subs ? 'Subtitle tersedia OK' : 'Tidak ada subtitle';
                        const vi = s.video_info || {};
                        if (vi.width && vi.height) document.getElementById('clInfoRes').textContent = `${vi.width}-${vi.height} (${vi.codec || 'mp4'})`;
                        if (vi.fps) document.getElementById('clInfoFps').textContent = `${vi.fps} fps`;
                        if (vi.size_mb) document.getElementById('clInfoSize').textContent = `${vi.size_mb} MB`;
                        document.getElementById('clCapNote').textContent = s.has_subs
                            ? 'OK Subtitle ditemukan - caption word-by-word siap digunakan.'
                            : 'Peringatan: Subtitle tidak tersedia - pilih "Tanpa caption" atau coba bahasa lain.';
                        document.getElementById('clManager').classList.remove('view-hidden');
                        clRenderClips();
                        showToast(`Analisa selesai: ${clClips.length} kandidat klip ditemukan`, 'success');
                    }, (err) => {
                        btn.disabled = false;
                        showToast(err || 'Analisa gagal', 'error');
                    }).catch(err => { btn.disabled = false; showToast(err || 'Analisa gagal', 'error'); });

                } else {
                    // BATCH mode - analisa satu per satu secara berurutan
                    const total = urlList.length;
                    let done = 0, failed = 0;
                    document.getElementById('clProgLbl').textContent = `Batch analisa: 0/${total} video...`;

                    for (let i = 0; i < urlList.length; i++) {
                        const url = urlList[i];
                        document.getElementById('clProgLbl').textContent = `Batch analisa video ${i+1}/${total}: ${url.split('/').pop().slice(0,40)}`;
                        document.getElementById('clProgPct').textContent = Math.round(i / total * 100) + '%';
                        document.getElementById('clProgBar').style.width = Math.round(i / total * 100) + '%';
                        try { await clAnalyzeOneUrl(url, isUrlMode); done++; }
                        catch (e) { failed++; console.warn(`Batch gagal: ${url} - ${e}`); }
                    }

                    btn.disabled = false;
                    document.getElementById('clProgPct').textContent = '100%';
                    document.getElementById('clProgBar').style.width = '100%';

                    if (!clClips.length) {
                        showToast('Semua video gagal dianalisa', 'error');
                        document.getElementById('clProgress').classList.add('view-hidden');
                        return;
                    }
                    // Info ringkasan batch
                    document.getElementById('clVideoInfo').classList.remove('view-hidden');
                    document.getElementById('clInfoDur').textContent = `Batch: ${done} video${failed > 0 ? ' (' + failed + ' gagal)' : ' OK'}`;
                    document.getElementById('clInfoFile').textContent = `${total} video`;
                    document.getElementById('clInfoSubs').textContent = '';
                    document.getElementById('clInfoRes').textContent = '';
                    document.getElementById('clInfoFps').textContent = '';
                    document.getElementById('clInfoSize').textContent = '';
                    document.getElementById('clCapNote').textContent = done > 0
                        ? `OK ${done} video berhasil dianalisa.${failed > 0 ? ' Peringatan: ' + failed + ' video gagal.' : ''}`
                        : 'Peringatan: Semua video gagal - cek koneksi atau URL.';
                    document.getElementById('clManager').classList.remove('view-hidden');
                    clRenderClips();
                    showToast(`Batch selesai: ${clClips.length} klip dari ${done} video`, 'success');
                }
            };


            // -- Clip Manager toolbar --
            document.getElementById('clSelAll').onclick = () => { clClips.forEach(c => c.selected = true); clRenderClips(); };
            document.getElementById('clSelNone').onclick = () => { clClips.forEach(c => c.selected = false); clRenderClips(); };
            document.getElementById('clSelInvert').onclick = () => { clClips.forEach(c => c.selected = !c.selected); clRenderClips(); };
            document.getElementById('clDelSel').onclick = () => {
                const before = clClips.length;
                clClips = clClips.filter(c => !c.selected);
                clRenderClips();
                showToast(`${before - clClips.length} klip dihapus`, 'info');
            };
            // Custom crop slider + letterbox
            const clCropPosSel = document.getElementById('clCropPos');
            clCropPosSel.onchange = () => {
                document.getElementById('clCropSliderWrap').classList.toggle('view-hidden',
                    clCropPosSel.value !== 'custom');
            };
            document.getElementById('clCropSlider').oninput = (e) => {
                document.getElementById('clCropSliderVal').textContent = e.target.value + '%';
            };

            // -- Face Tracker: checkbox + mode dropdown --
            const clFaceEnableChk = document.getElementById('clFaceEnable');
            const clFaceModeWrap  = document.getElementById('clFaceModeWrap');
            const clFaceModeSel   = document.getElementById('clFaceMode');
            const clFaceModeNote  = document.getElementById('clFaceModeNote');

            clFaceEnableChk.onchange = () => {
                clFaceModeWrap.classList.toggle('view-hidden', !clFaceEnableChk.checked);
                if (!clFaceEnableChk.checked) clFaceModeSel.value = 'smart'; // reset
            };

            const faceModeNotes = {
                smart:  ' OpenCV deteksi wajah per klip -> FFmpeg crop otomatis ke posisi wajah pembicara.',
                split:  ' Video dibagi 2 statis: atas & bawah. Cocok untuk podcast / debate 2 orang.',
                gaming: ' 60% atas = konten utama, 40% bawah = facecam sudut kanan bawah.',
            };
            clFaceModeSel.onchange = () => {
                const note = faceModeNotes[clFaceModeSel.value] || '';
                clFaceModeNote.textContent = note;
                clFaceModeNote.style.display = note ? '' : 'none';
            };

            // Cancel / Stop job berjalan
            document.getElementById('clCancelBtn').onclick = () => {
                if (!clCurrentJobId) return showToast('Tidak ada job berjalan', 'info');
                api(`/api/youtube/job/${clCurrentJobId}/stop`, 'POST', {}).then(d => {
                    showToast(d.message || d.error || 'Job dibatalkan', d.error ? 'error' : 'info');
                    clCurrentJobId = null;
                });
            };

            document.getElementById('clCsvBtn').onclick = () => {
                if (!clLastCsv) return showToast('Export dulu untuk membuat rekap CSV', 'info');
                const cbtn = document.getElementById('clCsvBtn');
                cbtn.disabled = true;
                api('/api/downloader/convert_csv', 'POST', {
                    csv_path: clLastCsv,
                    platforms: ['youtube', 'tiktok', 'facebook', 'instagram', 'threads', 'x', 'pinterest', 'shopee'],
                }).then(d => {
                    cbtn.disabled = false;
                    if (d.success) showToast('CSV siap upload semua platform dibuat di folder hasil OK', 'success');
                    else showToast(d.error || 'Gagal membuat CSV upload', 'error');
                }).catch(() => { cbtn.disabled = false; showToast('Gagal terhubung ke server', 'error'); });
            };
            document.getElementById('clAddManual').onclick = () => {
                const dur = clVideoDuration || 60;
                clClips.push({ start: 0, end: Math.min(45, dur), label: `Manual ${clClips.length + 1}`, selected: true });
                clRenderClips();
                // Scroll to bottom
                const list = document.getElementById('clClipList');
                list.scrollTop = list.scrollHeight;
            };

            // -- FASE 2: Export --
            document.getElementById('clExportBtn').onclick = () => {
                const selected = clClips.filter(c => c.selected);
                if (!selected.length) { showToast('Pilih minimal 1 klip di Clip Manager', 'error'); return; }
                if (!clVideoPath) { showToast('Jalankan Analisa dulu', 'error'); return; }
                const btn = document.getElementById('clExportBtn');
                btn.disabled = true;
                clShowProgress('Memulai export...', 0);
                const payload = {
                    video: clVideoPath,
                    srt: clSrtPath,
                    clips: selected.map(c => ({ start: c.start, end: c.end, label: c.label, video: c.video || '' })),
                    ratio: document.getElementById('clRatio').value,
                    crop_pos: document.getElementById('clCropPos').value,
                    crop_custom_pct: (parseInt(document.getElementById('clCropSlider').value, 10) || 50) / 100,
                    letterbox: document.getElementById('clLetterbox').checked,
                    face_mode: document.getElementById('clFaceEnable').checked
                                  ? document.getElementById('clFaceMode').value
                                  : 'off',
                    caption_style: document.getElementById('clCapStyle').value,
                    caption_size: parseInt(document.getElementById('clCapSize').value, 10) || 15,
                    caption_color: document.getElementById('clCapColor').value,
                    caption_outline: document.getElementById('clCapOutline').value,
                    caption_pos: document.getElementById('clCapPos').value,
                    bgm: document.getElementById('clBgm').value.trim(),
                    bgm_vol: parseInt(document.getElementById('clBgmVol').value, 10) || 30,
                    orig_vol: parseInt(document.getElementById('clOrigVol').value, 10) || 100,
                    bgm_fade_in: document.getElementById('clBgmFadeIn').checked,
                    bgm_fade_out: document.getElementById('clBgmFadeOut').checked,
                    anti_detect: document.getElementById('clAntiDetect').checked,
                    smart_meta: document.getElementById('clSmartMeta').checked,
                    turbo: document.getElementById('clTurbo').checked,
                    name_pattern: document.getElementById('clNamePattern').value.trim() || 'clip_{n}',
                    title: '',
                };
                api('/api/clipper/export', 'POST', payload).then(d => {
                    if (!d.success) {
                        btn.disabled = false;
                        showToast(d.error || 'Gagal memulai export', 'error');
                        document.getElementById('clProgress').classList.add('view-hidden');
                        return;
                    }
                    clPollJob(d.job_id, s => {
                        btn.disabled = false;
                        if (s.success) {
                            clLastCsv = s.csv_path || '';
                            document.getElementById('clCsvBtn').classList.toggle('view-hidden', !clLastCsv);
                            const nUp = (s.upload_csv_files || []).length;
                            showToast(nUp
                                ? `Export selesai: ${s.clips || 0} klip + ${nUp} CSV siap-upload otomatis! `
                                : `Export selesai: ${s.clips || 0} klip siap upload! `, 'success');
                        } else {
                            showToast(s.error || 'Export gagal', 'error');
                        }
                    });
                }).catch(() => { btn.disabled = false; showToast('Gagal terhubung ke server', 'error'); });
            };
        }

        let downloaderInitialized = false;

        function initDownloaderView() {
            if (downloaderInitialized) return;
            downloaderInitialized = true;

            const dlPlatform = document.getElementById('dlPlatform');
            const dlRegionWrap = document.getElementById('dlRegionWrap');
            const btnDownloaderStart = document.getElementById('btnDownloaderStart');
            const btnDownloaderOpenFolder = document.getElementById('btnDownloaderOpenFolder');
            const dlProgressPanel = document.getElementById('dlProgressPanel');
            const dlProgLabel = document.getElementById('dlProgLabel');
            const dlProgPct = document.getElementById('dlProgPct');
            const dlProgFill = document.getElementById('dlProgFill');
            const dlProgMsg = document.getElementById('dlProgMsg');
            let dlPollTimer = null;
            let dlCurrentJobId = null;
            document.getElementById('dlStopBtn').onclick = () => {
                if (!dlCurrentJobId) return showToast('Tidak ada download berjalan', 'info');
                api(`/api/youtube/job/${dlCurrentJobId}/stop`, 'POST', {}).then(d => {
                    showToast(d.message || d.error || 'Stop dikirim', d.error ? 'error' : 'info');
                });
            };

            function populateDlLinkPhones() {
                const sel = document.getElementById('dlLinkPhone');
                sel.innerHTML = '';
                sel.add(new Option('Memuat HP...', ''));
                fetch('/api/downloader/phones').then(r => r.json()).then(d => {
                    sel.innerHTML = '';
                    if (!d.has_adb) { sel.add(new Option('adb.exe tidak ada', '')); return; }
                    const devs = d.devices || [];
                    if (!devs.length) { sel.add(new Option('Tidak ada HP terhubung', '')); return; }
                    devs.forEach(dev => sel.add(new Option(dev.serial, dev.serial)));
                }).catch(() => { sel.innerHTML = ''; sel.add(new Option('Gagal cek HP', '')); });
            }

            // Platform yang HARUS pakai profil GAM yang sudah login (Instagram &
            // Threads mengunci akses tanpa login bahkan utk 1 link/post publik).
            const DL_NEEDS_PROFILE_LINK = ['instagram', 'threads'];
            const DL_NEEDS_PROFILE_ACC  = ['facebook', 'instagram', 'threads', 'twitter', 'pinterest'];

            function populateProfileSelect(selectId, platform) {
                const sel = document.getElementById(selectId);
                sel.innerHTML = '';
                sel.add(new Option('Memuat profil...', ''));
                fetch('/api/profiles').then(r => r.json()).then(d => {
                    sel.innerHTML = '';
                    const all = d.profiles || [];
                    const groupKey = platform === 'twitter' ? 'X' : (platform.charAt(0).toUpperCase() + platform.slice(1));
                    let matched = all.filter(p => (p.groups || []).some(g => g.toLowerCase() === groupKey.toLowerCase()));
                    const list = matched.length ? matched : all;
                    if (!list.length) { sel.add(new Option('Tidak ada profil', '')); return; }
                    list.forEach(p => sel.add(new Option(p.name, p.name)));
                }).catch(() => { sel.innerHTML = ''; sel.add(new Option('Gagal memuat profil', '')); });
            }

            function syncDownloaderUI() {
                const isShopee = dlPlatform.value === 'shopee';
                dlRegionWrap.classList.toggle('view-hidden', !isShopee);
                document.getElementById('dlShopeePhotoRow').classList.toggle('view-hidden', !isShopee);
                const needsProfile = DL_NEEDS_PROFILE_LINK.includes(dlPlatform.value);
                document.getElementById('dlLinkProfileWrap').classList.toggle('view-hidden', !needsProfile);
                if (needsProfile) populateProfileSelect('dlLinkProfile', dlPlatform.value);
            }

            document.getElementById('dlLinkAff').onchange = (e) => {
                const on = e.target.checked;
                document.getElementById('dlLinkPhoneWrap').classList.toggle('view-hidden', !on);
                if (on) populateDlLinkPhones();
            };
            document.getElementById('dlLinkPhoneRefresh').onclick = populateDlLinkPhones;

            dlPlatform.onchange = syncDownloaderUI;
            syncDownloaderUI();

            // -- Mode: Dari Link vs Per Akun (username) --
            let dlMode = 'link';
            const dlAccPlatform = document.getElementById('dlAccPlatform');

            function populateDlAccPhones() {
                const sel = document.getElementById('dlAccPhone');
                sel.innerHTML = '';
                sel.add(new Option('Memuat HP...', ''));
                fetch('/api/downloader/phones').then(r => r.json()).then(d => {
                    sel.innerHTML = '';
                    if (!d.has_adb) { sel.add(new Option('adb.exe tidak ada (butuh ShoveBot)', '')); return; }
                    const devs = d.devices || [];
                    if (!devs.length) { sel.add(new Option('Tidak ada HP terhubung', '')); return; }
                    devs.forEach(dev => sel.add(new Option(dev.serial, dev.serial)));
                }).catch(() => { sel.innerHTML = ''; sel.add(new Option('Gagal cek HP', '')); });
            }

            const DL_ACC_HINTS = {
                tiktok:    { label: 'Username TikTok', ph: '@username atau https://www.tiktok.com/@username' },
                youtube:   { label: 'Channel YouTube', ph: '@handle atau https://www.youtube.com/@channel' },
                instagram: { label: 'Username Instagram', ph: '@username atau https://www.instagram.com/username' },
                facebook:  { label: 'Halaman/Username Facebook', ph: 'username atau https://www.facebook.com/namahalaman' },
                threads:   { label: 'Username Threads', ph: '@username atau https://www.threads.com/@username' },
                twitter:   { label: 'Username X / Twitter', ph: '@username atau https://x.com/username' },
                pinterest: { label: 'Username Pinterest', ph: 'username atau https://www.pinterest.com/username' },
            };

            function syncDlAccUI() {
                const platform = dlAccPlatform.value;
                const isShopee = platform === 'shopee';
                const needsProfile = DL_NEEDS_PROFILE_ACC.includes(platform);
                document.getElementById('dlAccPhoneWrap').classList.toggle('view-hidden', !isShopee);
                document.getElementById('dlAccRegionWrap').classList.toggle('view-hidden', !isShopee);
                document.getElementById('dlAccLinksWrap').classList.toggle('view-hidden', !isShopee);
                document.getElementById('dlShopeeWarn').classList.toggle('view-hidden', !isShopee);
                document.getElementById('dlAccProfileWrap').classList.toggle('view-hidden', !needsProfile);
                document.getElementById('dlAccProfileNote').classList.toggle('view-hidden', !needsProfile);
                if (needsProfile) populateProfileSelect('dlAccProfile', platform);
                // Jenis media (video/gambar) hanya untuk platform yt-dlp, bukan Shopee
                document.getElementById('dlAccMediaWrap').classList.toggle('view-hidden', isShopee);
                if (isShopee) {
                    document.getElementById('dlAccTargetLabel').innerHTML = 'Link share Shopee Video <small>(dari HP: Bagikan -> Salin tautan)</small>';
                    document.getElementById('dlAccTarget').placeholder = 'Tempel link share (mis. https://id.shp.ee/xxxx)';
                    populateDlAccPhones();
                } else {
                    const h = DL_ACC_HINTS[platform] || DL_ACC_HINTS.tiktok;
                    document.getElementById('dlAccTargetLabel').innerHTML = h.label;
                    document.getElementById('dlAccTarget').placeholder = h.ph;
                }
            }
            document.getElementById('dlAccProfileRefresh').onclick = () => populateProfileSelect('dlAccProfile', dlAccPlatform.value);
            document.getElementById('dlLinkProfileRefresh').onclick = () => populateProfileSelect('dlLinkProfile', dlPlatform.value);
            dlAccPlatform.onchange = syncDlAccUI;
            document.getElementById('dlAccPhoneRefresh').onclick = populateDlAccPhones;
            syncDlAccUI();

            document.querySelectorAll('#dlModeToggle .scrape-mode-btn').forEach(btn => {
                btn.onclick = () => {
                    document.querySelectorAll('#dlModeToggle .scrape-mode-btn').forEach(b => b.classList.remove('active'));
                    btn.classList.add('active');
                    dlMode = btn.dataset.mode;
                    const isAcc = dlMode === 'account';
                    document.getElementById('dlAccountPanel').classList.toggle('view-hidden', !isAcc);
                    document.getElementById('dlLinkRow').classList.toggle('view-hidden', isAcc);
                    document.getElementById('dlUrlsRow').classList.toggle('view-hidden', isAcc);
                    if (isAcc) syncDlAccUI();
                };
            });

            // Konverter CSV -> format siap upload
            document.getElementById('btnConvertCsv').onclick = () => {
                const csv_path = document.getElementById('dlConvertCsv').value.trim();
                if (!csv_path) { showToast('Isi path CSV sumber dulu', 'error'); return; }
                const platforms = [...document.querySelectorAll('#dlConvertPlats input:checked')].map(c => c.value);
                if (!platforms.length) { showToast('Pilih minimal satu platform', 'error'); return; }
                const btn = document.getElementById('btnConvertCsv');
                btn.disabled = true;
                document.getElementById('dlConvertMsg').textContent = 'Memproses...';
                api('/api/downloader/convert_csv', 'POST', { csv_path, platforms }).then(d => {
                    btn.disabled = false;
                    if (!d.success) { document.getElementById('dlConvertMsg').textContent = ''; showToast(d.error || 'Gagal konversi', 'error'); return; }
                    const names = (d.files || []).map(f => f.split(/[\\/]/).pop()).join(', ');
                    document.getElementById('dlConvertMsg').textContent =
                        `${d.files.length} file dibuat (${d.count} baris): ${names}\nDi folder: ${d.folder}`;
                    showToast(`${d.files.length} CSV upload dibuat`, 'success');
                }).catch(() => { btn.disabled = false; showToast('Gagal terhubung ke server', 'error'); });
            };

            btnDownloaderOpenFolder.onclick = () => {
                const accPlat = dlAccPlatform.value === 'twitter' ? 'x' : dlAccPlatform.value;
                api('/api/downloader/open_folder', 'POST', {
                    platform: dlMode === 'account'
                        ? accPlat + '_akun'
                        : dlPlatform.value,
                }).then(d => {
                    if (!d.success) showToast(d.error || 'Gagal membuka folder download', 'error');
                }).catch(() => showToast('Gagal terhubung ke server', 'error'));
            };

            btnDownloaderStart.onclick = () => {
                if (dlMode === 'account') {
                    const target = document.getElementById('dlAccTarget').value.trim();
                    if (!target) { showToast('Isi username atau link profil dulu', 'error'); return; }
                    const isShopee = dlAccPlatform.value === 'shopee';
                    const payload = {
                        platform: dlAccPlatform.value,
                        target,
                        limit: (function () { const v = parseInt(document.getElementById('dlAccLimit').value, 10); return isNaN(v) ? 10 : Math.max(0, v); })(),
                    };
                    if (isShopee) {
                        payload.device = document.getElementById('dlAccPhone').value;
                        if (!payload.device) { showToast('Pilih HP yang terhubung dulu', 'error'); return; }
                        payload.region = document.getElementById('dlAccRegion').value;
                        payload.with_links = document.getElementById('dlAccLinks').checked;
                    } else {
                        payload.media_type = document.getElementById('dlAccMedia').value;
                    }
                    if (DL_NEEDS_PROFILE_ACC.includes(dlAccPlatform.value)) {
                        payload.profile = document.getElementById('dlAccProfile').value;
                        if (!payload.profile) { showToast('Pilih profil GAM yang sudah login dulu', 'error'); return; }
                    }
                    btnDownloaderStart.disabled = true;
                    revealProgressPanel(dlProgressPanel);
                    api('/api/downloader/account', 'POST', payload).then(res => {
                        if (res.success && res.job_id) pollDownloaderJob(res.job_id);
                        else {
                            btnDownloaderStart.disabled = false;
                            showToast(res.error || 'Gagal memulai download', 'error');
                        }
                    }).catch(() => {
                        btnDownloaderStart.disabled = false;
                        showToast('Gagal terhubung ke server', 'error');
                    });
                    return;
                }
                const urls = document.getElementById('dlUrls').value.split('\n').map(s => s.trim()).filter(Boolean);
                if (!urls.length) {
                    showToast('Masukkan minimal satu URL media atau link produk', 'error');
                    return;
                }
                btnDownloaderStart.disabled = true;
                revealProgressPanel(dlProgressPanel);
                const dlPayload = {
                    platform: dlPlatform.value,
                    region: document.getElementById('dlRegion').value,
                    media_type: document.getElementById('dlMediaType').value,
                    with_meta: document.getElementById('dlWithMeta').checked,
                    urls,
                };
                if (dlPlatform.value === 'shopee') {
                    dlPayload.photo_limit = parseInt(document.getElementById('dlPhotoLimit').value, 10) || 0;
                    if (document.getElementById('dlLinkAff').checked) {
                        dlPayload.with_links = true;
                        dlPayload.device = document.getElementById('dlLinkPhone').value;
                        if (!dlPayload.device) { btnDownloaderStart.disabled = false; showToast('Pilih HP untuk link affiliate', 'error'); return; }
                    }
                }
                if (DL_NEEDS_PROFILE_LINK.includes(dlPlatform.value)) {
                    dlPayload.profile = document.getElementById('dlLinkProfile').value;
                    if (!dlPayload.profile) { btnDownloaderStart.disabled = false; showToast('Pilih profil GAM yang sudah login dulu', 'error'); return; }
                }
                api('/api/downloader/download', 'POST', dlPayload).then(res => {
                    if (res.success && res.job_id) {
                        pollDownloaderJob(res.job_id);
                    } else {
                        btnDownloaderStart.disabled = false;
                        showToast(res.error || 'Gagal memulai download', 'error');
                    }
                }).catch(() => {
                    btnDownloaderStart.disabled = false;
                    showToast('Gagal terhubung ke server', 'error');
                });
            };

            function pollDownloaderJob(jobId) {
                dlCurrentJobId = jobId;
                if (dlPollTimer) clearTimeout(dlPollTimer);
                api(`/api/youtube/job/${jobId}`, 'GET').then(res => {
                    dlProgLabel.textContent = res.message || 'Memproses...';
                    const pct = res.pct || 0;
                    dlProgPct.textContent = pct + '%';
                    dlProgFill.style.width = pct + '%';
                    const files = (res.files || []).map((f, i) => `${i + 1}. ${f}`).join('\n');
                    dlProgMsg.textContent = (res.error || '') + (files ? `\n${files}` : '');
                    if (res.done) {
                        btnDownloaderStart.disabled = false;
                        dlCurrentJobId = null;
                        if (res.stopped) {
                            showToast(res.message || 'Download dihentikan', 'info');
                        } else if (res.success) {
                            showToast('Download selesai!', 'success');
                            // Tampilkan konverter CSV + auto-isi path CSV hasil
                            const panel = document.getElementById('dlConvertPanel');
                            panel.classList.remove('view-hidden');
                            if (res.csv) document.getElementById('dlConvertCsv').value = res.csv;
                        } else showToast(res.error || 'Download gagal', 'error');
                    } else {
                        dlPollTimer = setTimeout(() => pollDownloaderJob(jobId), 1500);
                    }
                }).catch(() => { btnDownloaderStart.disabled = false; });
            }
        }

        // ---------- Generate View ----------
        let generateInitialized = false;

        function initGenerateView() {
            if (generateInitialized) return;
            generateInitialized = true;

            const subBtns = document.querySelectorAll('#generateSubNav .sub-tab-btn:not(.btn-settings-icon)');
            subBtns.forEach(sub => {
                sub.onclick = () => {
                    subBtns.forEach(b => b.classList.remove('active'));
                    sub.classList.add('active');

                    const subId = sub.dataset.sub;
                    document.getElementById('sub-scraper').classList.toggle('view-hidden', subId !== 'scraper');
                    document.getElementById('sub-generator').classList.toggle('view-hidden', subId !== 'generator');
                    document.getElementById('sub-promptgen').classList.toggle('view-hidden', subId !== 'promptgen');
                    document.getElementById('sub-vidprompt').classList.toggle('view-hidden', subId !== 'vidprompt');

                    if (subId === 'generator') loadGeneratorProfiles();
                    if (subId === 'scraper') loadImageGeneratorProfiles();
                };
            });

            // --- PROMPT GENERATOR (gambar -> prompt via Gemini Vision) ---
            const PG_STYLES = [
                ['hard_selling', 'Hard Selling (Iklan TV)'],
                ['soft_selling', 'Soft Selling / Storytelling'],
                ['storyboard', 'Storyboard (Multi-Adegan)'],
                ['aesthetic', 'Aesthetic / Cinematic (Non-Jualan)'],
                ['edukasi', 'Edukasi / Tips (Non-Jualan)'],
                ['pov', 'POV / Relatable (Non-Jualan)'],
                ['asmr', 'ASMR / Satisfying (Non-Jualan)'],
            ];
            const TIKTOK_GO_STYLES = [
                ['tiktok_go_wisata', 'TikTok Go - Tempat Wisata'],
                ['tiktok_go_kuliner', 'TikTok Go - Kuliner'],
                ['tiktok_go_hotel', 'TikTok Go - Hotel / Penginapan'],
            ];
            const ALL_PROMPT_STYLES = [...PG_STYLES, ...TIKTOK_GO_STYLES];
            const PG_PLATFORMS = [
                ['tiktok', 'TikTok'],
                ['shopee', 'Shopee Video'],
                ['instagram', 'Instagram Reels'],
                ['youtube', 'YouTube Shorts'],
                ['umum', 'Umum'],
            ];
            function addPromptStyleOptions(sel, options) {
                options.forEach(([v, l]) => sel.add(new Option(l, v)));
            }
            addPromptStyleOptions(document.getElementById('imgGenApStyle'), ALL_PROMPT_STYLES);
            addPromptStyleOptions(document.getElementById('genApStyle'), ALL_PROMPT_STYLES);
            addPromptStyleOptions(document.getElementById('pgStyle'), ALL_PROMPT_STYLES);
            document.querySelectorAll('select.ap-platform, #pgPlatform').forEach(sel => {
                PG_PLATFORMS.forEach(([v, l]) => sel.add(new Option(l, v)));
            });
            function bindTikTokGoPlatform(styleId, platformId) {
                const styleSel = document.getElementById(styleId);
                const platformSel = document.getElementById(platformId);
                if (!styleSel || !platformSel) return;
                styleSel.onchange = () => {
                    if (styleSel.value.startsWith('tiktok_go_')) {
                        platformSel.value = 'tiktok';
                    }
                };
            };
            bindTikTokGoPlatform('imgGenApStyle', 'imgGenApPlatform');
            bindTikTokGoPlatform('genApStyle', 'genApPlatform');
            bindTikTokGoPlatform('pgStyle', 'pgPlatform');

            function requestPrompt(payload, btn, onDone) {
                const oldHtml = btn.innerHTML;
                btn.disabled = true;
                btn.innerHTML = '<i class="fa-solid fa-spinner fa-spin"></i> Menganalisa gambar...';
                api('/api/generate/prompt', 'POST', payload).then(res => {
                    btn.disabled = false;
                    btn.innerHTML = oldHtml;
                    if (res.success && res.prompt) {
                        onDone(res.prompt);
                        showToast('Prompt berhasil dibuat!', 'success');
                    } else {
                        showToast(res.error || 'Gagal generate prompt', 'error');
                    }
                }).catch(() => {
                    btn.disabled = false;
                    btn.innerHTML = oldHtml;
                    showToast('Gagal terhubung ke server', 'error');
                });
            }

            const storyboardScenesByNiche = {
                wisata: [
                    { title: 'Hook lokasi', note: 'opening memperlihatkan identitas tempat, suasana utama, dan alasan pertama untuk berhenti scroll', overlay: 'Hidden gem yang wajib masuk list', camera: 'push-in pelan dari wide shot menuju spot utama' },
                    { title: 'Detail pengalaman', note: 'menunjukkan aktivitas, spot foto, tekstur lokasi, dan suasana yang membuat tempat terasa hidup', overlay: 'Spotnya cakep banget', camera: 'pan lembut mengikuti detail tempat dan elemen paling menarik' },
                    { title: 'Closing rekomendasi', note: 'penutup dengan momen paling memorable dan ajakan simpan untuk itinerary', overlay: 'Simpan buat trip berikutnya', camera: 'pull-back sinematik yang memberi ruang untuk teks penutup' },
                ],
                kuliner: [
                    { title: 'Hook menu', note: 'opening makanan/minuman terlihat menggugah selera dan langsung jelas menu andalannya', overlay: 'Menu ini bikin penasaran', camera: 'push-in pelan ke piring atau gelas utama' },
                    { title: 'Detail rasa', note: 'close-up tekstur, topping, saus, uap, potongan, atau momen saat makanan siap disantap', overlay: 'Detailnya menggoda', camera: 'macro move pelan melintasi tekstur makanan' },
                    { title: 'Closing tempat', note: 'penutup memperlihatkan ambience tempat makan dan alasan penonton ingin mencoba', overlay: 'Masuk list kuliner kamu', camera: 'tilt/pull-back lembut dari menu ke suasana meja atau tempat' },
                ],
                hotel: [
                    { title: 'Hook staycation', note: 'opening memperlihatkan kamar/fasad/lobby sebagai kesan pertama yang nyaman', overlay: 'Staycation nyaman di sini', camera: 'push-in bersih seperti room tour premium' },
                    { title: 'Detail fasilitas', note: 'menonjolkan view, kasur, kamar mandi, kolam, lobby, balkon, atau detail interior yang relevan', overlay: 'Detailnya bikin betah', camera: 'slide pelan mengikuti fasilitas utama' },
                    { title: 'Closing mood', note: 'penutup menunjukkan suasana menginap yang rileks dan ajakan simpan rekomendasi', overlay: 'Simpan buat liburan nanti', camera: 'pull-back hangat dengan ambience tenang' },
                ],
                affiliate: [
                    { title: 'Hook masalah', note: 'opening memperlihatkan situasi relatable sebelum produk hadir sebagai solusi natural', overlay: 'Sering ngalamin ini?', camera: 'push-in ringan ke situasi atau produk utama' },
                    { title: 'Detail produk', note: 'menonjolkan detail bentuk, bahan, fitur visual, cara pakai, dan manfaat yang aman tanpa overclaim', overlay: 'Detailnya praktis', camera: 'macro move pelan pada fitur produk' },
                    { title: 'Closing CTA', note: 'penutup memperlihatkan hasil pemakaian atau produk tertata jelas dengan CTA halus', overlay: 'Cek sebelum kehabisan', camera: 'pull-back komersial dari produk ke komposisi akhir' },
                ],
                umum: [
                    { title: 'Hook pembuka', note: 'opening memperkenalkan objek/tempat dengan visual paling kuat dan membuat penonton penasaran', overlay: 'Lihat ini dulu', camera: 'push-in pelan dari wide shot ke subjek utama' },
                    { title: 'Detail utama', note: 'menampilkan detail paling menarik, tekstur, suasana, atau momen yang menjawab rasa penasaran', overlay: 'Bagian ini menarik', camera: 'pan/slide lembut mengikuti detail utama' },
                    { title: 'Closing cerita', note: 'penutup merangkum mood dan menyambungkan konten menjadi satu cerita utuh', overlay: 'Simpan inspirasinya', camera: 'pull-back sinematik untuk memberi akhir yang rapi' },
                ],
            };

            function storyboardNicheFromStyle(style, platform) {
                if (style === 'tiktok_go_wisata') return 'wisata';
                if (style === 'tiktok_go_kuliner') return 'kuliner';
                if (style === 'tiktok_go_hotel') return 'hotel';
                if (platform === 'shopee' || style === 'hard_selling' || style === 'soft_selling') return 'affiliate';
                return 'umum';
            }

            function storyboardMetaFor(niche, idx, total) {
                const pack = storyboardScenesByNiche[niche] || storyboardScenesByNiche.umum;
                if (idx === 0) return pack[0];
                if (idx === total - 1) return pack[2];
                if (total === 3) return pack[1];
                return {
                    title: `Bridge cerita ${idx + 1}`,
                    note: `${pack[1].note}; variasikan angle agar transisi dari scene ${idx} ke scene ${idx + 2} terasa natural`,
                    overlay: pack[1].overlay,
                    camera: pack[1].camera,
                };
            }

            function cleanPromptText(text) {
                return String(text || '').replace(/\s+/g, ' ').trim();
            }

            function safeSlug(text, fallback = 'konten') {
                const slug = String(text || '')
                    .normalize('NFKD')
                    .replace(/[\u0300-\u036f]/g, '')
                    .replace(/[^a-zA-Z0-9]+/g, '_')
                    .replace(/^_+|_+$/g, '')
                    .toLowerCase();
                return (slug || fallback).slice(0, 70);
            }

            function inferStoryName(opts) {
                const manual = cleanPromptText(opts.storyName || '');
                if (manual) return manual;
                const context = cleanPromptText(opts.note || '');
                if (context) return context.split(/[.,;-]/)[0].slice(0, 70);
                return 'konten';
            }

            const storyboardCtaPacks = {
                tiktok_go: [
                    'Amankan promonya dengan cara klik tag lokasi di bawah',
                    'Cek promonya lewat tag lokasi di bawah',
                    'Klik tag lokasi di bawah sebelum promonya berubah',
                    'Simpan dulu, lalu cek tag lokasi di bawah',
                ],
                shopee_aff: [
                    'Klik keranjang kuning sebelum kehabisan',
                    'Cek produknya lewat keranjang kuning',
                    'Simpan dulu, lalu cek keranjang kuningnya',
                    'Kalau butuh, langsung cek link produknya',
                ],
                tiktok: [
                    'Follow untuk rekomendasi berikutnya',
                    'Tulis di komentar kalau mau part berikutnya',
                    'Simpan video ini biar tidak lupa',
                    'Bagikan ke teman yang butuh rekomendasi ini',
                ],
                sosmed: [
                    'Simpan dan bagikan ke teman kamu',
                    'Like kalau ini membantu',
                    'Bagikan ke teman yang harus lihat ini',
                    'Simpan dulu buat referensi nanti',
                ],
                umum: [
                    'Simpan dulu buat inspirasi berikutnya',
                    'Bagikan kalau konten ini berguna',
                    'Ikuti untuk ide berikutnya',
                    'Cek detailnya sebelum kamu tentukan pilihan',
                ],
            };

            function resolveCtaType(rawType, niche, platform) {
                if (rawType && rawType !== 'auto') return rawType;
                if (niche === 'wisata' || niche === 'kuliner' || niche === 'hotel') return 'tiktok_go';
                if (niche === 'affiliate' || platform === 'shopee') return 'shopee_aff';
                if (platform === 'tiktok') return 'tiktok';
                return 'sosmed';
            }

            function pickStoryboardCta(opts, niche) {
                if (opts.ctaType === 'custom' && cleanPromptText(opts.customCta)) {
                    return cleanPromptText(opts.customCta);
                }
                const type = resolveCtaType(opts.ctaType || 'auto', niche, opts.platform);
                const pack = storyboardCtaPacks[type] || storyboardCtaPacks.umum;
                return pack[Math.floor(Math.random() * pack.length)];
            }

            // Simpan deskripsi visual hasil analisis Gemini per scene referensi
            let sceneRefDescriptions = []; // [{index, summary, subject, colors, composition, mood, ok}]

            function buildStoryboardScenes(basePrompt, opts) {
                const total = Math.max(3, Math.min(12, parseInt(opts.count, 10) || 3));
                const duration = opts.duration || '8s';
                const niche = storyboardNicheFromStyle(opts.style, opts.platform);
                const context = cleanPromptText(opts.note);
                const storyName = inferStoryName(opts);
                const storySlug = safeSlug(storyName, niche || 'konten');
                const finalCta = pickStoryboardCta(opts, niche);
                const scenes = [];
                for (let i = 0; i < total; i += 1) {
                    const meta = storyboardMetaFor(niche, i, total);
                    const nextMeta = i < total - 1 ? storyboardMetaFor(niche, i + 1, total) : null;
                    const sceneNo = i + 1;
                    const isLast = sceneNo === total;
                    const title = `Scene ${sceneNo}/${total} - ${storyName} - ${meta.title}`;
                    const overlayText = isLast ? finalCta : meta.overlay;
                    const connect = nextMeta
                        ? `Warna, tempat, dan mood harus nyambung dengan scene ${sceneNo + 1}: ${nextMeta.title}.`
                        : `Tutup dengan CTA Bahasa Indonesia: "${finalCta}".`;

                    // Gunakan deskripsi visual Gemini jika tersedia untuk scene ini
                    const refDesc = sceneRefDescriptions[i];
                    const hasVisual = refDesc && refDesc.ok && refDesc.summary;

                    // === IMAGE PROMPT (Nano Banana) ===
                    // Prinsip: prompt pendek & konkret > instruksi panjang abstrak.
                    // Deskripsi visual spesifik dari gambar referensi diletakkan di DEPAN
                    // agar Nano Banana membaca konteks visual sebelum instruksi gaya.
                    const visualAnchor = hasVisual
                        ? refDesc.summary
                        : `subjek utama dari foto referensi scene ${sceneNo}`;
                    const colorAnchor = (hasVisual && refDesc.colors)
                        ? `Warna dominan: ${refDesc.colors}.`
                        : '';
                    const ctaPart = isLast ? ` Sisipkan teks CTA Bahasa Indonesia: "${finalCta}".` : '';
                    const contextPart = context ? ` Konteks: ${context}.` : '';

                    const imagePrompt = cleanPromptText([
                        `${title}.`,
                        `SATU FOTO untuk scene ini saja — bukan kolase, bukan grid.`,
                        `Tampilkan: ${visualAnchor}.`,
                        colorAnchor,
                        `Gaya scene: ${meta.note}.`,
                        connect,
                        ctaPart,
                        contextPart,
                        `Fotorealistik, vertikal 9:16, sinematik, ${niche || 'konten sosmed'}.`,
                        basePrompt ? `Detail tambahan: ${basePrompt.slice(0, 200)}.` : '',
                    ].filter(Boolean).join(' '));

                    // === VIDEO PROMPT (image-to-video) ===
                    const videoPrompt = cleanPromptText([
                        `${title}. Video Bahasa Indonesia, durasi ${duration.replace('s', '')} detik.`,
                        `Jadikan foto scene ini frame awal. ${meta.note}.`,
                        `Kamera: ${meta.camera}. Overlay: "${overlayText}".`,
                        `Pertahankan ${hasVisual ? refDesc.subject : 'subjek utama'}, warna, dan komposisi foto ini.`,
                        connect,
                        'Fotorealistik, sinematik, vertikal, TikTok/Reels/Shorts.',
                    ].join(' '));

                    scenes.push({
                        scene: sceneNo,
                        total,
                        title,
                        note: isLast ? `${meta.note}; CTA: ${finalCta}` : meta.note,
                        overlay: overlayText,
                        cta: isLast ? finalCta : '',
                        duration,
                        imagePrompt,
                        videoPrompt,
                        outputName: `scene_${String(sceneNo).padStart(2, '0')}_${storySlug}`,
                        refVisual: hasVisual ? refDesc.summary : '',
                    });
                }
                return scenes;
            }

            // Analisis foto referensi via Gemini Vision sebelum generate
            async function analyzeSceneRefs(imagePaths, onProgress) {
                sceneRefDescriptions = [];
                if (!imagePaths || !imagePaths.length) return;
                try {
                    const res = await fetch('/api/generate/analyze_scene_refs', {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify({ image_paths: imagePaths }),
                    });
                    const data = await res.json();
                    if (data.success && data.descriptions) {
                        sceneRefDescriptions = data.descriptions;
                        if (onProgress) onProgress(data.message || `${data.descriptions.filter(d => d.ok).length}/${data.descriptions.length} foto dianalisis.`);
                    } else {
                        if (onProgress) onProgress(data.error || 'Analisis referensi gagal, prompt akan tetap dibuat tanpa deskripsi visual.');
                    }
                } catch (e) {
                    if (onProgress) onProgress('Analisis referensi tidak tersedia, lanjut generate.');
                }
            }


            function syncImgStoryboardControls() {
                const on = document.getElementById('imgGenStoryboardMode').checked;
                const countEl = document.getElementById('imgGenSceneCount');
                const durEl = document.getElementById('imgGenSceneDuration');
                const noteEl = document.getElementById('imgGenStoryboardNote');
                [countEl, durEl, noteEl].forEach(el => { if (el) el.disabled = !on; });
                if (on) {
                    const refs = (typeof imgGenReferenceImages !== 'undefined') ? imgGenReferenceImages : [];
                    const count = refs.length >= 3 ? refs.length : Math.max(3, parseInt(countEl.value, 10) || 3);
                    countEl.value = count;
                    document.getElementById('imgGenRuns').value = count;
                    document.getElementById('imgGenRatio').value = '9:16';
                    document.getElementById('genDuration').value = durEl.value;
                }
            }
            document.getElementById('imgGenStoryboardMode').onchange = syncImgStoryboardControls;
            document.getElementById('imgGenSceneCount').oninput = syncImgStoryboardControls;
            document.getElementById('imgGenSceneDuration').onchange = syncImgStoryboardControls;
            function bindCtaWrap(selectId, wrapId) {
                const sel = document.getElementById(selectId);
                const wrap = document.getElementById(wrapId);
                if (!sel || !wrap) return;
                const sync = () => wrap.classList.toggle('view-hidden', sel.value !== 'custom');
                sel.onchange = sync;
                sync();
            }
            bindCtaWrap('imgGenCtaType', 'imgGenCustomCtaWrap');
            bindCtaWrap('genCtaType', 'genCustomCtaWrap');
            syncImgStoryboardControls();

            // Auto Prompt di Video Generator
            document.getElementById('btnGenAutoPrompt').onclick = () => {
                const imagePath = document.getElementById('genImagePath').value.trim();
                if (!imagePath) { showToast('Pilih file gambar utama dulu', 'error'); return; }
                requestPrompt({
                    image_path: imagePath,
                    mode: 'video',
                    style: document.getElementById('genApStyle').value,
                    platform: document.getElementById('genApPlatform').value,
                    lang: 'id',
                    duration: document.getElementById('genDuration').value,
                }, document.getElementById('btnGenAutoPrompt'), prompt => {
                    document.getElementById('genPromptText').value = prompt;
                });
            };

            // Auto Prompt di Image Generator
            document.getElementById('btnImgGenAutoPrompt').onclick = () => {
                const imagePath = document.getElementById('imgGenImagePath').value.trim();
                if (!imagePath) { showToast('Pilih file gambar referensi dulu', 'error'); return; }
                requestPrompt({
                    image_path: imagePath,
                    mode: 'image',
                    style: document.getElementById('imgGenApStyle').value,
                    platform: document.getElementById('imgGenApPlatform').value,
                    lang: 'id',
                    extra: document.getElementById('imgGenStoryboardMode').checked
                        ? 'Buat basis visual untuk paket storyboard konten. Wajib bedakan wajah model dari orang asli bila ada manusia, ubah warna pakaian utama, dan semua teks/efek tulisan harus Bahasa Indonesia.'
                        : '',
                }, document.getElementById('btnImgGenAutoPrompt'), prompt => {
                    document.getElementById('imgGenPromptText').value = prompt;
                });
            };

            // Panel Prompt Generator
            const pgMode = document.getElementById('pgMode');
            pgMode.onchange = () => {
                document.getElementById('pgDurationWrap').style.visibility =
                    pgMode.value === 'video' ? 'visible' : 'hidden';
            };
            document.getElementById('btnPgGenerate').onclick = () => {
                const imagePath = document.getElementById('pgImagePath').value.trim();
                if (!imagePath) { showToast('Pilih file gambar dulu', 'error'); return; }
                requestPrompt({
                    image_path: imagePath,
                    mode: pgMode.value,
                    style: document.getElementById('pgStyle').value,
                    platform: document.getElementById('pgPlatform').value,
                    lang: document.getElementById('pgLang').value,
                    duration: document.getElementById('pgDuration').value,
                    extra: document.getElementById('pgExtra').value.trim(),
                }, document.getElementById('btnPgGenerate'), prompt => {
                    document.getElementById('pgResult').value = prompt;
                    document.getElementById('pgResultWrap').classList.remove('view-hidden');
                });
            };
            document.getElementById('btnPgCopy').onclick = () => {
                const text = document.getElementById('pgResult').value;
                if (!text) return;
                navigator.clipboard.writeText(text).then(() => showToast('Prompt disalin!', 'success'));
            };
            function pgSendTo(subId, pathId, promptId) {
                const text = document.getElementById('pgResult').value.trim();
                if (!text) { showToast('Belum ada hasil prompt', 'error'); return; }
                const img = document.getElementById('pgImagePath').value.trim();
                document.getElementById(promptId).value = text;
                if (img) document.getElementById(pathId).value = img;
                const btn = document.querySelector(`#generateSubNav .sub-tab-btn[data-sub="${subId}"]`);
                if (btn) btn.click();
                showToast('Prompt terkirim!', 'success');
            }
            document.getElementById('btnPgToVideo').onclick = () => pgSendTo('generator', 'genImagePath', 'genPromptText');
            document.getElementById('btnPgToImage').onclick = () => pgSendTo('scraper', 'imgGenImagePath', 'imgGenPromptText');

            // --- TIRU VIDEO (Video -> Prompt) ---
            PG_PLATFORMS.forEach(([v, l]) => document.getElementById('vpPlatform').add(new Option(l, v)));

            document.getElementById('btnVpAnalyze').onclick = () => {
                const videoPath = document.getElementById('vpVideoPath').value.trim();
                if (!videoPath) { showToast('Pilih file video contoh dulu', 'error'); return; }
                const btn = document.getElementById('btnVpAnalyze');
                const oldHtml = btn.innerHTML;
                btn.disabled = true;
                btn.innerHTML = '<i class="fa-solid fa-spinner fa-spin"></i> Mengunggah & menganalisa video... (bisa 1-2 menit)';
                api('/api/generate/prompt_from_video', 'POST', {
                    video_path: videoPath,
                    platform: document.getElementById('vpPlatform').value,
                    lang: document.getElementById('vpLang').value,
                    duration: document.getElementById('vpDuration').value,
                    extra: document.getElementById('vpExtra').value.trim(),
                }).then(res => {
                    btn.disabled = false;
                    btn.innerHTML = oldHtml;
                    if (res.success) {
                        document.getElementById('vpObservation').textContent = res.observation ? ('Pengamatan: ' + res.observation) : '';
                        document.getElementById('vpImagePrompt').value = res.image_prompt || '';
                        document.getElementById('vpVideoPrompt').value = res.video_prompt || '';
                        document.getElementById('vpResultWrap').classList.remove('view-hidden');
                        showToast('Prompt dari video berhasil dibuat!', 'success');
                    } else {
                        showToast(res.error || 'Gagal menganalisa video', 'error');
                    }
                }).catch(() => {
                    btn.disabled = false;
                    btn.innerHTML = oldHtml;
                    showToast('Gagal terhubung ke server', 'error');
                });
            };

            const vpCopy = (id) => {
                const t = document.getElementById(id).value;
                if (!t) return;
                navigator.clipboard.writeText(t).then(() => showToast('Prompt disalin!', 'success'));
            };
            document.getElementById('btnVpCopyImage').onclick = () => vpCopy('vpImagePrompt');
            document.getElementById('btnVpCopyVideo').onclick = () => vpCopy('vpVideoPrompt');

            document.getElementById('btnVpToImage').onclick = () => {
                const t = document.getElementById('vpImagePrompt').value.trim();
                if (!t) { showToast('Belum ada prompt foto', 'error'); return; }
                document.getElementById('imgGenPromptText').value = t;
                document.querySelector('#generateSubNav .sub-tab-btn[data-sub="scraper"]').click();
                showToast('Prompt foto terkirim. Pilih gambar referensi lalu Generate.', 'success');
            };
            document.getElementById('btnVpToVideo').onclick = () => {
                const t = document.getElementById('vpVideoPrompt').value.trim();
                if (!t) { showToast('Belum ada prompt video', 'error'); return; }
                document.getElementById('genPromptText').value = t;
                document.querySelector('#generateSubNav .sub-tab-btn[data-sub="generator"]').click();
                showToast('Prompt video terkirim. Pilih gambar utama lalu Generate.', 'success');
            };

            // --- IMAGE GENERATOR LOGIC ---
            const imgGenProfilesList = document.getElementById('imgGenProfilesList');
            const imgGenSelectAll = document.getElementById('imgGenSelectAllProfiles');
            const imgGenSelectedCount = document.getElementById('imgGenSelectedCount');
            const imgGenProfileSearch = document.getElementById('imgGenProfileSearch');
            const btnImgGenStart = document.getElementById('btnImgGenStart');
            const btnImgGenStop = document.getElementById('btnImgGenStop');
            const btnImgGenCheckCredits = document.getElementById('btnImgGenCheckCredits');
            const imgGenProgressPanel = document.getElementById('imgGenProgressPanel');
            const imgGenProgLabel = document.getElementById('imgGenProgLabel');
            const imgGenProgPct = document.getElementById('imgGenProgPct');
            const imgGenProgFill = document.getElementById('imgGenProgFill');
            const imgGenProgMsg = document.getElementById('imgGenProgMsg');
            let imgGenPollTimer = null;
            let currentImgGenJobId = null;

            function loadImageGeneratorProfiles() {
                renderImageGeneratorProfilesList();
            }

            function renderImageGeneratorProfilesList() {
                imgGenProfilesList.innerHTML = '';
                const term = imgGenProfileSearch.value.toLowerCase();
                allProfiles.forEach(p => {
                    if (term && !p.name.toLowerCase().includes(term)) return;
                    const item = document.createElement('label');
                    item.className = 'profile-check-item';
                    item.innerHTML = `
                        <input type="checkbox" class="img-gen-profile-checkbox" value="${p.name}" checked>
                        <span>${escapeHtml(p.name.replace('_gmail.com', ''))}</span>
                    `;
                    item.querySelector('input').onchange = updateImgGenSelectedCount;
                    imgGenProfilesList.appendChild(item);
                });
                updateImgGenSelectedCount();
            }

            function updateImgGenSelectedCount() {
                const checks = imgGenProfilesList.querySelectorAll('.img-gen-profile-checkbox:checked');
                imgGenSelectedCount.textContent = `${checks.length} terpilih`;
            }

            imgGenProfileSearch.oninput = renderImageGeneratorProfilesList;
            imgGenSelectAll.onchange = () => {
                imgGenProfilesList.querySelectorAll('.img-gen-profile-checkbox').forEach(c => c.checked = imgGenSelectAll.checked);
                updateImgGenSelectedCount();
            };

            let imgGenReferenceImages = [];

            function normalizeImagePaths(paths) {
                return (paths || [])
                    .map(p => String(p || '').trim())
                    .filter(p => p && /\.(jpe?g|png|webp)$/i.test(p));
            }

            function clonePayload(payload) {
                return JSON.parse(JSON.stringify(payload || {}));
            }

            function isSuccessRow(row) {
                return !!row && (row.success === true || String(row.status || '').toLowerCase() === 'success');
            }

            function firstRowFile(row, re) {
                return ((row && row.files) || []).find(f => re.test(String(f || ''))) || '';
            }

            function rowVariantIndex(row, fallback) {
                const raw = row && row.variant_index;
                const n = Number(raw);
                return Number.isFinite(n) ? n : fallback;
            }

            function uniquePaths(paths) {
                const seen = new Set();
                return (paths || []).filter(p => {
                    p = String(p || '').trim();
                    if (!p || seen.has(p)) return false;
                    seen.add(p);
                    return true;
                });
            }

            function renderImgGenSceneImages() {
                const wrap = document.getElementById('imgGenSceneImageList');
                const count = document.getElementById('imgGenSceneImageCount');
                count.textContent = `${imgGenReferenceImages.length} foto`;
                if (!imgGenReferenceImages.length) {
                    wrap.innerHTML = '<div class="rp-file-empty">Belum ada foto scene. Untuk storyboard, tambahkan minimal 3 foto.</div>';
                    return;
                }
                wrap.innerHTML = imgGenReferenceImages.map((p, i) => `
                    <div class="rp-file-item">
                        <div class="rp-file-icon">${i + 1}</div>
                        <div class="rp-file-main">
                            <div class="rp-file-name">Scene ${i + 1}</div>
                            <div class="rp-file-path">${escapeHtml(p)}</div>
                        </div>
                        <div class="rp-toolbar-actions">
                            <button type="button" class="btn-secondary neutral-lite img-scene-up" data-idx="${i}" ${i === 0 ? 'disabled' : ''} title="Naik"><i class="fa-solid fa-arrow-up"></i></button>
                            <button type="button" class="btn-secondary neutral-lite img-scene-down" data-idx="${i}" ${i === imgGenReferenceImages.length - 1 ? 'disabled' : ''} title="Turun"><i class="fa-solid fa-arrow-down"></i></button>
                            <button type="button" class="btn-secondary img-scene-remove" data-idx="${i}" title="Hapus"><i class="fa-solid fa-xmark"></i></button>
                        </div>
                    </div>
                `).join('');
                wrap.querySelectorAll('.img-scene-remove').forEach(btn => {
                    btn.onclick = () => {
                        imgGenReferenceImages.splice(parseInt(btn.dataset.idx, 10), 1);
                        document.getElementById('imgGenImagePath').value = imgGenReferenceImages[0] || '';
                        syncImgStoryboardControls();
                        renderImgGenSceneImages();
                    };
                });
                wrap.querySelectorAll('.img-scene-up').forEach(btn => {
                    btn.onclick = () => {
                        const i = parseInt(btn.dataset.idx, 10);
                        if (i > 0) {
                            [imgGenReferenceImages[i - 1], imgGenReferenceImages[i]] = [imgGenReferenceImages[i], imgGenReferenceImages[i - 1]];
                            document.getElementById('imgGenImagePath').value = imgGenReferenceImages[0] || '';
                            renderImgGenSceneImages();
                        }
                    };
                });
                wrap.querySelectorAll('.img-scene-down').forEach(btn => {
                    btn.onclick = () => {
                        const i = parseInt(btn.dataset.idx, 10);
                        if (i < imgGenReferenceImages.length - 1) {
                            [imgGenReferenceImages[i + 1], imgGenReferenceImages[i]] = [imgGenReferenceImages[i], imgGenReferenceImages[i + 1]];
                            document.getElementById('imgGenImagePath').value = imgGenReferenceImages[0] || '';
                            renderImgGenSceneImages();
                        }
                    };
                });
            }

            function setImgGenReferenceImages(paths, opts = {}) {
                const incoming = normalizeImagePaths(paths);
                if (opts.replace !== false) imgGenReferenceImages = [];
                incoming.forEach(p => {
                    if (!imgGenReferenceImages.includes(p)) imgGenReferenceImages.push(p);
                });
                if (imgGenReferenceImages.length) {
                    document.getElementById('imgGenImagePath').value = imgGenReferenceImages[0];
                }
                if (opts.storyboard || imgGenReferenceImages.length >= 3) {
                    document.getElementById('imgGenStoryboardMode').checked = true;
                }
                syncImgStoryboardControls();
                renderImgGenSceneImages();
            }

            function getImgGenReferenceImages() {
                const fromField = document.getElementById('imgGenImagePath').value.trim();
                const refs = imgGenReferenceImages.length ? imgGenReferenceImages.slice() : normalizeImagePaths([fromField]);
                if (!refs.length && fromField) refs.push(fromField);
                return refs;
            }

            window.gamaSetImageGeneratorReferences = function(paths, opts = {}) {
                setImgGenReferenceImages(paths, opts);
                return imgGenReferenceImages.length;
            };

            document.getElementById('imgGenImagePath').addEventListener('change', () => {
                const p = document.getElementById('imgGenImagePath').value.trim();
                if (p) setImgGenReferenceImages([p], { replace: !document.getElementById('imgGenStoryboardMode').checked });
            });
            document.getElementById('btnImgGenSceneAdd').onclick = async () => {
                if (!(window.pywebview && window.pywebview.api && window.pywebview.api.pick_files)) {
                    showToast('Pilih banyak foto tersedia di mode desktop GAMA', 'info');
                    return;
                }
                try {
                    const files = await window.pywebview.api.pick_files('image');
                    setImgGenReferenceImages(files || [], { replace: false, storyboard: true });
                } catch (e) { showToast('Gagal membuka dialog foto', 'error'); }
            };
            document.getElementById('btnImgGenSceneClear').onclick = () => {
                imgGenReferenceImages = [];
                document.getElementById('imgGenImagePath').value = '';
                renderImgGenSceneImages();
                syncImgStoryboardControls();
            };
            renderImgGenSceneImages();

            btnImgGenStart.onclick = async () => {
                const selectedProfiles = Array.from(imgGenProfilesList.querySelectorAll('.img-gen-profile-checkbox:checked')).map(c => c.value);
                if (!selectedProfiles.length) { showToast('Pilih minimal satu Google Profile', 'error'); return; }
                const referenceImages = getImgGenReferenceImages();
                const imagePath = referenceImages[0] || document.getElementById('imgGenImagePath').value.trim();
                const prompt = document.getElementById('imgGenPromptText').value.trim();
                if (!imagePath) { showToast('Pilih file gambar referensi', 'error'); return; }
                if (!prompt) { showToast('Isi prompt image dulu', 'error'); return; }
                const storyboardMode = document.getElementById('imgGenStoryboardMode').checked;
                let storyboardScenes = [];
                if (storyboardMode) {
                    if (referenceImages.length < 3) {
                        showToast('Storyboard butuh minimal 3 foto referensi: 1 scene = 1 foto.', 'error');
                        return;
                    }
                    syncImgStoryboardControls();
                    // === ANALISIS REFERENSI via Gemini Vision ===
                    // Analisis dulu setiap foto referensi untuk mendapatkan deskripsi visual
                    // konkret yang akan disisipkan ke prompt agar Nano Banana lebih akurat.
                    btnImgGenStart.disabled = true;
                    btnImgGenStop.classList.remove('view-hidden');
                    revealProgressPanel(imgGenProgressPanel);
                    imgGenProgLabel.textContent = 'Menganalisis foto referensi...';
                    imgGenProgMsg.textContent = 'Gemini Vision sedang membaca setiap foto referensi untuk membuat prompt yang lebih akurat...';
                    imgGenProgFill.style.width = '5%';
                    imgGenProgPct.textContent = '5%';
                    await analyzeSceneRefs(referenceImages, (msg) => {
                        imgGenProgMsg.textContent = msg;
                    });
                    imgGenProgLabel.textContent = 'Membangun prompt scene...';
                    imgGenProgFill.style.width = '15%';
                    imgGenProgPct.textContent = '15%';
                    storyboardScenes = buildStoryboardScenes(prompt, {
                        count: referenceImages.length,
                        duration: document.getElementById('imgGenSceneDuration').value,
                        style: document.getElementById('imgGenApStyle').value,
                        platform: document.getElementById('imgGenApPlatform').value,
                        note: document.getElementById('imgGenStoryboardNote').value,
                        storyName: document.getElementById('imgGenStoryboardName').value,
                        ctaType: document.getElementById('imgGenCtaType').value,
                        customCta: document.getElementById('imgGenCustomCta').value,
                    });
                }
                const imgConcurrency = Math.max(1, parseInt(document.getElementById('imgGenConcurrency').value, 10) || 1);
                const payload = {
                    profiles: selectedProfiles,
                    image_path: imagePath,
                    prompt: storyboardScenes.length ? storyboardScenes[0].imagePrompt : prompt,
                    prompt_variants: storyboardScenes.map(s => s.imagePrompt),
                    image_variants: storyboardScenes.length ? referenceImages.slice(0, storyboardScenes.length) : [],
                    scene_titles: storyboardScenes.map(s => s.title),
                    output_names: storyboardScenes.map(s => s.outputName),
                    ratio: document.getElementById('imgGenRatio').value,
                    duration: storyboardScenes.length ? storyboardScenes[0].duration : '8s',
                    model: document.getElementById('imgGenModel').value,
                    runs: storyboardScenes.length ? storyboardScenes.length : document.getElementById('imgGenRuns').value,
                    concurrency: storyboardScenes.length ? Math.min(imgConcurrency, selectedProfiles.length, storyboardScenes.length) : document.getElementById('imgGenConcurrency').value,
                    hidden: document.getElementById('imgGenHidden').checked,
                    mode: storyboardScenes.length ? 'single' : (selectedProfiles.length > 1 ? 'batch' : 'single')
                };
                pendingImgStoryboardScenes = storyboardScenes;
                lastImgGenPayload = clonePayload(payload);
                currentImgGenRetryContext = null;
                resetImgGenResult();
                btnImgGenStart.disabled = true;
                btnImgGenStop.classList.remove('view-hidden');
                revealProgressPanel(imgGenProgressPanel);
                imgGenProgLabel.textContent = 'Memulai job generate image...';
                imgGenProgMsg.textContent = 'Menghubungi server Nano Banana...';
                api('/api/generate-image', 'POST', payload).then(res => {
                    if (res.job_id) {
                        currentImgGenJobId = res.job_id;
                        pollImgGenJob(res.job_id);
                    } else {
                        btnImgGenStart.disabled = false;
                        btnImgGenStop.classList.add('view-hidden');
                        showToast(res.error || 'Gagal memulai image generation', 'error');
                    }
                }).catch(() => {
                    btnImgGenStart.disabled = false;
                    btnImgGenStop.classList.add('view-hidden');
                    showToast('Gagal terhubung ke server', 'error');
                });
            };

            function pollImgGenJob(jobId) {
                if (imgGenPollTimer) clearTimeout(imgGenPollTimer);
                api(`/api/youtube/job/${jobId}`, 'GET').then(res => {
                    imgGenProgLabel.textContent = res.message || 'Memproses...';
                    const pct = res.pct || 0;
                    imgGenProgPct.textContent = pct + '%';
                    imgGenProgFill.style.width = pct + '%';
                    let msg = res.message || '';
                    if (res.error) msg += '\nERROR: ' + res.error;
                    if (res.rows && res.rows.length) msg += '\n' + res.rows.map((r, i) => `${i + 1}. ${r.title}: ${r.status}`).join('\n');
                    imgGenProgMsg.textContent = msg;
                    if (res.done) {
                        btnImgGenStart.disabled = false;
                        btnImgGenStop.classList.add('view-hidden');
                        const retryContext = currentImgGenRetryContext;
                        currentImgGenRetryContext = null;
                        if (retryContext) mergeImgGenRetryResult(res, retryContext);
                        else showImgGenResult(res.files || [], res.rows || []);
                        if (res.success) {
                            showToast('Generate image selesai!', 'success');
                        } else {
                            showToast(res.error || 'Generate image gagal', 'error');
                        }
                    } else {
                        imgGenPollTimer = setTimeout(() => pollImgGenJob(jobId), 1500);
                    }
                }).catch(() => {
                    btnImgGenStart.disabled = false;
                    btnImgGenStop.classList.add('view-hidden');
                });
            }

            // Foto hasil yang terakhir dibuat (untuk auto-lanjut ke Video Generator)
            let lastImgGenFiles = [];
            let pendingImgStoryboardScenes = [];
            let lastImgStoryboardScenes = [];
            let lastImgGenPayload = null;
            let imgGenSceneResults = [];
            let currentImgGenRetryContext = null;

            function resetImgGenResult() {
                lastImgGenFiles = [];
                lastImgStoryboardScenes = [];
                imgGenSceneResults = [];
                currentImgGenRetryContext = null;
                const panel = document.getElementById('imgGenResultPanel');
                const thumbs = document.getElementById('imgGenResultThumbs');
                const sceneList = document.getElementById('imgGenStoryboardList');
                const storyBtn = document.getElementById('btnImgGenStoryboardToVideo');
                if (thumbs) thumbs.innerHTML = '';
                if (sceneList) {
                    sceneList.innerHTML = '';
                    sceneList.classList.add('view-hidden');
                }
                if (storyBtn) storyBtn.classList.add('view-hidden');
                if (panel) panel.classList.add('view-hidden');
            }

            function buildImgGenSceneResults(files, rows) {
                const rowMap = new Map();
                (rows || []).forEach((row, i) => rowMap.set(rowVariantIndex(row, i), row));
                return pendingImgStoryboardScenes.map((scene, i) => {
                    const row = rowMap.get(i);
                    const file = firstRowFile(row, /\.(jpe?g|png|webp)$/i) || '';
                    const fallbackFile = (!row && files[i]) ? files[i] : '';
                    const imagePath = (isSuccessRow(row) ? file : '') || fallbackFile;
                    return {
                        ...scene,
                        imagePath,
                        status: imagePath ? 'success' : 'failed',
                        error: imagePath ? '' : ((row && (row.error || row.status)) || 'Scene gagal dibuat.'),
                    };
                });
            }

            function renderImgGenResultPanel() {
                const panel = document.getElementById('imgGenResultPanel');
                const thumbs = document.getElementById('imgGenResultThumbs');
                const sceneList = document.getElementById('imgGenStoryboardList');
                const storyBtn = document.getElementById('btnImgGenStoryboardToVideo');
                const heading = document.getElementById('imgGenResultHeading');
                const subtitle = document.getElementById('imgGenResultSubtitle');
                const accountHint = document.getElementById('imgGenAccountSwitchHint');
                const failedIndexes = imgGenSceneResults
                    .map((s, i) => (s && s.status !== 'success') ? i : -1)
                    .filter(i => i >= 0);
                const successCount = imgGenSceneResults.filter(s => s && s.status === 'success').length;
                const totalCount = imgGenSceneResults.length;

                if (!lastImgGenFiles.length && !imgGenSceneResults.length) {
                    panel.classList.add('view-hidden');
                    return;
                }

                // Update heading & subtitle berdasarkan hasil
                if (totalCount > 0 && failedIndexes.length > 0) {
                    if (heading) heading.innerHTML = `<i class="fa-solid fa-triangle-exclamation" style="color:#f5a623"></i> Foto Selesai ${successCount}/${totalCount}`;
                    if (subtitle) subtitle.textContent = `${successCount} dari ${totalCount} scene berhasil dibuat. Klik Retry pada scene yang gagal, atau ganti akun dulu jika terkena limit.`;
                    if (accountHint) accountHint.classList.remove('view-hidden');
                } else {
                    if (heading) heading.innerHTML = `<i class="fa-solid fa-circle-check"></i> Foto Selesai Dibuat`;
                    if (subtitle) subtitle.textContent = 'Foto hasil siap dimasukkan ke Video Generator. Prompt video akan dibuat otomatis dari foto ini, lalu tinggal klik GENERATE.';
                    if (accountHint) accountHint.classList.add('view-hidden');
                }

                thumbs.innerHTML = lastImgGenFiles.map(f =>
                    `<img src="/api/preview_image?path=${encodeURIComponent(f)}" alt="hasil" class="img-result-thumb" title="${escapeHtml(f)}">`
                ).join('');

                if (imgGenSceneResults.length) {
                    lastImgStoryboardScenes = imgGenSceneResults.filter(s => s && s.status === 'success' && s.imagePath);
                    sceneList.innerHTML = `
                        ${failedIndexes.length ? `
                            <div class="generator-actions" style="margin-bottom:0.55rem;">
                                <button type="button" class="btn-secondary" id="btnImgRetryAllFailed">
                                    <i class="fa-solid fa-rotate-right"></i> Generate ulang ${failedIndexes.length} scene gagal
                                </button>
                            </div>
                        ` : ''}
                        ${imgGenSceneResults.map((s, i) => `
                            <div class="rp-file-item">
                                <div class="rp-file-icon">${s.status === 'success' ? '<i class="fa-solid fa-check"></i>' : '<i class="fa-solid fa-triangle-exclamation"></i>'}</div>
                                <div class="rp-file-main">
                                    <div class="rp-file-name">${escapeHtml(s.title)}</div>
                                    <div class="rp-file-path">${escapeHtml(s.status === 'success' ? (s.imagePath || s.note) : (s.error || s.note))}</div>
                                </div>
                                ${s.status === 'success' ? '' : `
                                    <div class="rp-toolbar-actions">
                                        <button type="button" class="btn-secondary img-retry-scene" data-idx="${i}" title="Generate ulang scene ini">
                                            <i class="fa-solid fa-rotate-right"></i> Retry
                                        </button>
                                    </div>
                                `}
                            </div>
                        `).join('')}
                    `;
                    sceneList.classList.remove('view-hidden');
                    storyBtn.classList.toggle('view-hidden', lastImgStoryboardScenes.length < 3 || failedIndexes.length > 0);
                    sceneList.querySelectorAll('.img-retry-scene').forEach(btn => {
                        btn.onclick = () => retryImgGenScenes([parseInt(btn.dataset.idx, 10)]);
                    });
                    const retryAll = document.getElementById('btnImgRetryAllFailed');
                    if (retryAll) retryAll.onclick = () => retryImgGenScenes(failedIndexes);
                } else {
                    lastImgStoryboardScenes = [];
                    sceneList.innerHTML = '';
                    sceneList.classList.add('view-hidden');
                    storyBtn.classList.add('view-hidden');
                }
                panel.classList.remove('view-hidden');
            }

            function showImgGenResult(files, rows = []) {
                files = (files || []).filter(f => /\.(jpe?g|png|webp)$/i.test(f));
                if (pendingImgStoryboardScenes.length) {
                    imgGenSceneResults = buildImgGenSceneResults(files, rows);
                    lastImgGenFiles = imgGenSceneResults.map(s => s.imagePath).filter(Boolean);
                } else {
                    imgGenSceneResults = [];
                    lastImgGenFiles = files;
                }
                renderImgGenResultPanel();
            }

            function mergeImgGenRetryResult(res, context) {
                const files = (res.files || []).filter(f => /\.(jpe?g|png|webp)$/i.test(f));
                if (!imgGenSceneResults.length && pendingImgStoryboardScenes.length) {
                    imgGenSceneResults = pendingImgStoryboardScenes.map(scene => ({
                        ...scene,
                        imagePath: '',
                        status: 'failed',
                        error: 'Menunggu generate ulang.',
                    }));
                }
                (context.indexes || []).forEach((originalIdx, localIdx) => {
                    const row = (res.rows || [])[localIdx];
                    const file = firstRowFile(row, /\.(jpe?g|png|webp)$/i) || files[localIdx] || '';
                    const scene = pendingImgStoryboardScenes[originalIdx] || imgGenSceneResults[originalIdx];
                    if (!scene) return;
                    imgGenSceneResults[originalIdx] = {
                        ...scene,
                        imagePath: (isSuccessRow(row) && file) ? file : '',
                        status: (isSuccessRow(row) && file) ? 'success' : 'failed',
                        error: (isSuccessRow(row) && file) ? '' : ((row && (row.error || row.status)) || res.error || 'Generate ulang scene gagal.'),
                    };
                });
                lastImgGenFiles = uniquePaths(imgGenSceneResults.map(s => s && s.imagePath).filter(Boolean));
                renderImgGenResultPanel();
            }

            function retryImgGenScenes(indexes) {
                indexes = uniquePaths(indexes).map(i => parseInt(i, 10)).filter(i => Number.isFinite(i));
                if (!indexes.length) return;
                const selectedProfiles = Array.from(imgGenProfilesList.querySelectorAll('.img-gen-profile-checkbox:checked')).map(c => c.value);
                if (!selectedProfiles.length) { showToast('Pilih akun pengganti dulu di Image Generator.', 'error'); return; }
                const base = clonePayload(lastImgGenPayload);
                const promptVariants = indexes.map(i =>
                    (pendingImgStoryboardScenes[i] && pendingImgStoryboardScenes[i].imagePrompt)
                    || (base.prompt_variants || [])[i]
                    || base.prompt
                    || document.getElementById('imgGenPromptText').value.trim()
                );
                const imageVariants = indexes.map(i =>
                    (base.image_variants || [])[i]
                    || imgGenReferenceImages[i]
                    || base.image_path
                    || document.getElementById('imgGenImagePath').value.trim()
                );
                if (promptVariants.some(p => !p) || imageVariants.some(p => !p)) {
                    showToast('Data prompt/foto scene gagal tidak lengkap.', 'error');
                    return;
                }
                const concurrency = Math.max(1, parseInt(document.getElementById('imgGenConcurrency').value, 10) || 1);
                const payload = {
                    ...base,
                    profiles: selectedProfiles,
                    image_path: imageVariants[0],
                    image_variants: imageVariants,
                    prompt: promptVariants[0],
                    prompt_variants: promptVariants,
                    scene_titles: indexes.map(i => (pendingImgStoryboardScenes[i] && pendingImgStoryboardScenes[i].title) || `Scene ${i + 1}`),
                    output_names: indexes.map(i => (pendingImgStoryboardScenes[i] && pendingImgStoryboardScenes[i].outputName) || (base.output_names || [])[i] || `scene_${String(i + 1).padStart(2, '0')}_retry`),
                    ratio: document.getElementById('imgGenRatio').value,
                    duration: (pendingImgStoryboardScenes[indexes[0]] && pendingImgStoryboardScenes[indexes[0]].duration) || base.duration || '8s',
                    model: document.getElementById('imgGenModel').value,
                    runs: indexes.length,
                    concurrency: Math.min(concurrency, selectedProfiles.length, indexes.length),
                    hidden: document.getElementById('imgGenHidden').checked,
                    mode: 'single',
                };
                currentImgGenRetryContext = { indexes };
                btnImgGenStart.disabled = true;
                btnImgGenStop.classList.remove('view-hidden');
                revealProgressPanel(imgGenProgressPanel);
                showToast(`Generate ulang ${indexes.length} scene dimulai.`, 'info');
                api('/api/generate/image', 'POST', payload).then(res => {
                    if (res.success && res.job_id) {
                        currentImgGenJobId = res.job_id;
                        pollImgGenJob(res.job_id);
                    } else {
                        currentImgGenRetryContext = null;
                        btnImgGenStart.disabled = false;
                        btnImgGenStop.classList.add('view-hidden');
                        showToast(res.error || 'Gagal memulai retry scene', 'error');
                    }
                }).catch(() => {
                    currentImgGenRetryContext = null;
                    btnImgGenStart.disabled = false;
                    btnImgGenStop.classList.add('view-hidden');
                    showToast('Gagal terhubung ke server', 'error');
                });
            }

            document.getElementById('btnImgGenStart').onclick = () => {
                const selectedProfiles = Array.from(imgGenProfilesList.querySelectorAll('.img-gen-profile-checkbox:checked')).map(c => c.value);
                if (!selectedProfiles.length) { showToast('Pilih minimal satu Google Profile.', 'error'); return; }
                const payload = {
                    profiles: selectedProfiles,
                    prompt: document.getElementById('imgGenPromptText').value.trim(),
                    image_path: document.getElementById('imgGenImagePath').value.trim(),
                    ratio: document.getElementById('imgGenRatio').value,
                    duration: document.getElementById('imgGenDuration').value,
                    model: document.getElementById('imgGenModel').value,
                    runs: parseInt(document.getElementById('imgGenRuns').value, 10) || 1,
                    concurrency: parseInt(document.getElementById('imgGenConcurrency').value, 10) || 1,
                    hidden: document.getElementById('imgGenHidden').checked,
                    mode: 'bulk'
                };
                if (!payload.prompt || !payload.image_path) { showToast('Prompt dan foto referensi wajib diisi.', 'error'); return; }
                currentImgGenRetryContext = null;
                resetImgGenResult();
                btnImgGenStart.disabled = true;
                btnImgGenStop.classList.remove('view-hidden');
                revealProgressPanel(imgGenProgressPanel);
                imgGenProgLabel.textContent = 'Memulai job generate image...';
                imgGenProgMsg.textContent = 'Menghubungi server Nano Banana...';
                api('/api/generate-image', 'POST', payload).then(res => {
                    if (res.job_id) {
                        currentImgGenJobId = res.job_id;
                        pollImgGenJob(res.job_id);
                    } else {
                        btnImgGenStart.disabled = false;
                        btnImgGenStop.classList.add('view-hidden');
                        showToast(res.error || 'Gagal memulai image generation', 'error');
                    }
                }).catch(() => {
                    btnImgGenStart.disabled = false;
                    btnImgGenStop.classList.add('view-hidden');
                    showToast('Gagal terhubung ke server', 'error');
                });
            };

            document.getElementById('btnImgGenToVideo').onclick = () => {
                if (!lastImgGenFiles.length) {
                    showToast('Belum ada foto hasil yang sukses. Generate image sampai berhasil dulu.', 'error');
                    return;
                }
                const img = lastImgGenFiles[0];
                // Isi gambar utama Video Generator + pindah sub-tab
                document.getElementById('genImagePath').value = img;
                if (typeof setGenSceneImages === 'function') setGenSceneImages([img], true);
                const vbtn = document.querySelector('#generateSubNav .sub-tab-btn[data-sub="generator"]');
                if (vbtn) vbtn.click();
                // Prompt video dibuat otomatis dari foto ini (gaya/platform ikut Image Generator)
                const imageStyle = document.getElementById('imgGenApStyle').value;
                const imagePlatform = document.getElementById('imgGenApPlatform').value;
                const genStyleSel = document.getElementById('genApStyle');
                if ([...genStyleSel.options].some(opt => opt.value === imageStyle)) {
                    genStyleSel.value = imageStyle;
                } else if (imageStyle.startsWith('tiktok_go_')) {
                    const opt = new Option(
                        document.getElementById('imgGenApStyle').selectedOptions[0].textContent || imageStyle,
                        imageStyle
                    );
                    opt.hidden = true;
                    genStyleSel.add(opt);
                    genStyleSel.value = imageStyle;
                }
                document.getElementById('genApPlatform').value = imagePlatform;
                const scene = lastImgStoryboardScenes.find(s => s.imagePath === img) || lastImgStoryboardScenes[0];
                if (scene && scene.videoPrompt) {
                    document.getElementById('genDuration').value = scene.duration || document.getElementById('genDuration').value;
                    document.getElementById('genPromptText').value = scene.videoPrompt;
                    showToast(`${scene.title} terkirim ke Video Generator.`, 'success');
                    return;
                }
                showToast('Foto terkirim. Membuat prompt video otomatis...', 'success');
                requestPrompt({
                    image_path: img,
                    mode: 'video',
                    style: imageStyle,
                    platform: imagePlatform,
                    lang: 'id',
                    duration: document.getElementById('genDuration').value,
                }, document.getElementById('btnGenAutoPrompt'), prompt => {
                    document.getElementById('genPromptText').value = prompt;
                });
            };

            document.getElementById('btnImgGenOpenFolder').onclick = () => {
                if (!lastImgGenFiles.length) return;
                api('/api/reveal_folder', 'POST', { path: lastImgGenFiles[0] }).then(d => {
                    if (!d.success) showToast(d.error || 'Gagal membuka folder', 'error');
                }).catch(() => showToast('Gagal terhubung ke server', 'error'));
            };

            // Cek Folder permanen: buka folder hasil terakhir, fallback folder gambar referensi
            document.getElementById('btnImgGenCheckFolder').onclick = () => {
                const target = (lastImgGenFiles && lastImgGenFiles[0]) || document.getElementById('imgGenImagePath').value.trim();
                if (!target) { showToast('Pilih gambar referensi dulu', 'error'); return; }
                api('/api/reveal_folder', 'POST', { path: target }).then(d => {
                    if (!d.success) showToast(d.error || 'Gagal membuka folder', 'error');
                }).catch(() => showToast('Gagal terhubung ke server', 'error'));
            };

            btnImgGenStop.onclick = () => {
                if (!currentImgGenJobId) { showToast('Tidak ada proses yang berjalan', 'info'); return; }
                api(`/api/youtube/job/${currentImgGenJobId}/stop`, 'POST').then(res => {
                    if (res.success) showToast('Proses image generation dihentikan', 'info');
                    else showToast(res.error || 'Gagal menghentikan proses', 'error');
                }).catch(() => showToast('Gagal terhubung ke server', 'error'));
            };

            btnImgGenCheckCredits.onclick = () => {
                const selectedProfiles = Array.from(imgGenProfilesList.querySelectorAll('.img-gen-profile-checkbox:checked')).map(c => c.value);
                if (!selectedProfiles.length) { showToast('Pilih minimal satu Google Profile', 'error'); return; }
                btnImgGenCheckCredits.disabled = true;
                revealProgressPanel(imgGenProgressPanel);
                api('/api/generate/check_credits', 'POST', { profiles: selectedProfiles, model: 'Nano Banana' }).then(res => {
                    if (res.success && res.job_id) pollImgGenCreditJob(res.job_id);
                    else {
                        btnImgGenCheckCredits.disabled = false;
                        showToast(res.error || 'Gagal memulai cek limit', 'error');
                    }
                }).catch(() => {
                    btnImgGenCheckCredits.disabled = false;
                    showToast('Gagal terhubung ke server', 'error');
                });
            };

            function pollImgGenCreditJob(jobId) {
                api(`/api/youtube/job/${jobId}`, 'GET').then(res => {
                    imgGenProgLabel.textContent = res.message || 'Memeriksa...';
                    const pct = res.pct || 0;
                    imgGenProgPct.textContent = pct + '%';
                    imgGenProgFill.style.width = pct + '%';
                    imgGenProgMsg.textContent = (res.rows || []).map((r, i) => `${i + 1}. ${r.title}: ${r.status}`).join('\n');
                    if (res.done) {
                        btnImgGenCheckCredits.disabled = false;
                        if (res.success) showToast('Cek limit Nano Banana selesai', 'success');
                        else showToast(res.error || 'Cek limit gagal', 'error');
                    } else {
                        setTimeout(() => pollImgGenCreditJob(jobId), 1500);
                    }
                }).catch(() => { btnImgGenCheckCredits.disabled = false; });
            }

            // --- VIDEO GENERATOR LOGIC ---
            const genProfilesList = document.getElementById('genProfilesList');
            const genSelectAll = document.getElementById('genSelectAllProfiles');
            const genSelectedCount = document.getElementById('genSelectedCount');
            const genProfileSearch = document.getElementById('genProfileSearch');
            
            function loadGeneratorProfiles() {
                renderGeneratorProfilesList();
            }
            
            function renderGeneratorProfilesList() {
                genProfilesList.innerHTML = '';
                const term = genProfileSearch.value.toLowerCase();
                
                allProfiles.forEach(p => {
                    if (term && !p.name.toLowerCase().includes(term)) return;
                    
                    const item = document.createElement('label');
                    item.className = 'profile-check-item';
                    item.innerHTML = `
                        <input type="checkbox" class="gen-profile-checkbox" value="${p.name}" checked>
                        <span>${escapeHtml(p.name.replace('_gmail.com', ''))}</span>
                    `;
                    item.querySelector('input').onchange = updateGenSelectedCount;
                    genProfilesList.appendChild(item);
                });
                updateGenSelectedCount();
            }
            
            genProfileSearch.oninput = renderGeneratorProfilesList;
            
            genSelectAll.onchange = () => {
                const checks = genProfilesList.querySelectorAll('.gen-profile-checkbox');
                checks.forEach(c => c.checked = genSelectAll.checked);
                updateGenSelectedCount();
            };
            
            function updateGenSelectedCount() {
                const checks = genProfilesList.querySelectorAll('.gen-profile-checkbox:checked');
                genSelectedCount.textContent = `${checks.length} terpilih`;
            }
            
            const btnGenStart = document.getElementById('btnGenStart');
            const btnGenStop = document.getElementById('btnGenStop');
            const genProgressPanel = document.getElementById('genProgressPanel');
            const genProgLabel = document.getElementById('genProgLabel');
            const genProgPct = document.getElementById('genProgPct');
            const genProgFill = document.getElementById('genProgFill');
            const genProgMsg = document.getElementById('genProgMsg');
            
            let genPollTimer = null;
            let currentGenJobId = null;
            let lastGenFiles = [];
            let lastGenPayload = null;
            let genSceneResults = [];
            let currentGenRetryContext = null;
            let genSceneImages = [];

            function renderGenSceneImages() {
                const wrap = document.getElementById('genSceneImageList');
                const count = document.getElementById('genSceneImageCount');
                count.textContent = `${genSceneImages.length} foto`;
                if (!genSceneImages.length) {
                    wrap.innerHTML = '<div class="rp-file-empty">Opsional. Tambahkan 3+ foto kalau mau generate beberapa scene sekali jalan.</div>';
                    return;
                }
                wrap.innerHTML = genSceneImages.map((p, i) => `
                    <div class="rp-file-item">
                        <div class="rp-file-icon">${i + 1}</div>
                        <div class="rp-file-main">
                            <div class="rp-file-name">Scene ${i + 1}</div>
                            <div class="rp-file-path">${escapeHtml(p)}</div>
                        </div>
                        <div class="rp-toolbar-actions">
                            <button type="button" class="btn-secondary neutral-lite gen-scene-up" data-idx="${i}" ${i === 0 ? 'disabled' : ''} title="Naik"><i class="fa-solid fa-arrow-up"></i></button>
                            <button type="button" class="btn-secondary neutral-lite gen-scene-down" data-idx="${i}" ${i === genSceneImages.length - 1 ? 'disabled' : ''} title="Turun"><i class="fa-solid fa-arrow-down"></i></button>
                            <button type="button" class="btn-secondary gen-scene-remove" data-idx="${i}" title="Hapus"><i class="fa-solid fa-xmark"></i></button>
                        </div>
                    </div>
                `).join('');
                wrap.querySelectorAll('.gen-scene-remove').forEach(btn => {
                    btn.onclick = () => {
                        genSceneImages.splice(parseInt(btn.dataset.idx, 10), 1);
                        document.getElementById('genImagePath').value = genSceneImages[0] || '';
                        renderGenSceneImages();
                    };
                });
                wrap.querySelectorAll('.gen-scene-up').forEach(btn => {
                    btn.onclick = () => {
                        const i = parseInt(btn.dataset.idx, 10);
                        if (i > 0) {
                            [genSceneImages[i - 1], genSceneImages[i]] = [genSceneImages[i], genSceneImages[i - 1]];
                            document.getElementById('genImagePath').value = genSceneImages[0] || '';
                            renderGenSceneImages();
                        }
                    };
                });
                wrap.querySelectorAll('.gen-scene-down').forEach(btn => {
                    btn.onclick = () => {
                        const i = parseInt(btn.dataset.idx, 10);
                        if (i < genSceneImages.length - 1) {
                            [genSceneImages[i + 1], genSceneImages[i]] = [genSceneImages[i], genSceneImages[i + 1]];
                            document.getElementById('genImagePath').value = genSceneImages[0] || '';
                            renderGenSceneImages();
                        }
                    };
                });
            }

            function setGenSceneImages(paths, replace = true) {
                const incoming = normalizeImagePaths(paths);
                if (replace) genSceneImages = [];
                incoming.forEach(p => {
                    if (!genSceneImages.includes(p)) genSceneImages.push(p);
                });
                if (genSceneImages.length) document.getElementById('genImagePath').value = genSceneImages[0];
                renderGenSceneImages();
            }

            function getGenSceneImages() {
                const fromField = document.getElementById('genImagePath').value.trim();
                const refs = genSceneImages.length ? genSceneImages.slice() : normalizeImagePaths([fromField]);
                if (!refs.length && fromField) refs.push(fromField);
                return refs;
            }

            document.getElementById('genImagePath').addEventListener('change', () => {
                const p = document.getElementById('genImagePath').value.trim();
                if (p && !genSceneImages.length) setGenSceneImages([p], true);
            });
            document.getElementById('btnGenSceneAdd').onclick = async () => {
                if (!(window.pywebview && window.pywebview.api && window.pywebview.api.pick_files)) {
                    showToast('Pilih banyak foto tersedia di mode desktop GAMA', 'info');
                    return;
                }
                try {
                    const files = await window.pywebview.api.pick_files('image');
                    setGenSceneImages(files || [], false);
                } catch (e) { showToast('Gagal membuka dialog foto', 'error'); }
            };
            document.getElementById('btnGenSceneClear').onclick = () => {
                genSceneImages = [];
                renderGenSceneImages();
            };
            renderGenSceneImages();

            function ensureGenRetryPanel() {
                let panel = document.getElementById('genRetryPanel');
                if (!panel) {
                    panel = document.createElement('div');
                    panel.id = 'genRetryPanel';
                    panel.className = 'dl-convert view-hidden';
                    panel.style.marginTop = '1rem';
                    genProgressPanel.insertAdjacentElement('afterend', panel);
                }
                return panel;
            }

            function buildGenSceneResults(files, rows) {
                const rowMap = new Map();
                (rows || []).forEach((row, i) => rowMap.set(rowVariantIndex(row, i), row));
                const variantCount = Math.max(
                    (lastGenPayload && lastGenPayload.prompt_variants ? lastGenPayload.prompt_variants.length : 0),
                    (lastGenPayload && lastGenPayload.image_variants ? lastGenPayload.image_variants.length : 0),
                    (lastGenPayload && lastGenPayload.scene_titles ? lastGenPayload.scene_titles.length : 0)
                );
                if (!variantCount) return [];
                return Array.from({ length: variantCount }).map((_, i) => {
                    const row = rowMap.get(i);
                    const file = firstRowFile(row, /\.(mp4|mov|mkv|webm)$/i) || '';
                    const fallbackFile = (!row && files[i]) ? files[i] : '';
                    const videoPath = (isSuccessRow(row) ? file : '') || fallbackFile;
                    return {
                        index: i,
                        title: ((lastGenPayload && lastGenPayload.scene_titles) || [])[i] || `Scene ${i + 1}`,
                        videoPath,
                        status: videoPath ? 'success' : 'failed',
                        error: videoPath ? '' : ((row && (row.error || row.status)) || 'Scene video gagal dibuat.'),
                    };
                });
            }

            function renderGenRetryPanel() {
                const panel = ensureGenRetryPanel();
                const failedIndexes = genSceneResults
                    .map((s, i) => (s && s.status !== 'success') ? i : -1)
                    .filter(i => i >= 0);
                const successCount = genSceneResults.filter(s => s && s.status === 'success').length;
                const totalCount = genSceneResults.length;
                if (!genSceneResults.length || !failedIndexes.length) {
                    panel.classList.add('view-hidden');
                    panel.innerHTML = '';
                    return;
                }
                panel.innerHTML = `
                    <h4><i class="fa-solid fa-triangle-exclamation" style="color:#f5a623"></i> Scene Video Gagal &mdash; ${successCount}/${totalCount} Berhasil</h4>
                    <div style="margin:0.5rem 0;padding:0.55rem 0.8rem;background:rgba(255,170,0,0.1);border:1px solid rgba(255,170,0,0.35);border-radius:8px;display:flex;align-items:center;gap:0.5rem;font-size:0.82rem;color:var(--warning,#f5a623);">
                        <i class="fa-solid fa-circle-info"></i>
                        <span>Jika akun terkena limit atau error, <b>ganti centang akun</b> di panel kiri sebelum klik Retry &mdash; scene yang sudah berhasil tidak akan digenerate ulang.</span>
                    </div>
                    <div class="generator-actions" style="margin-top:0.55rem;margin-bottom:0.55rem;">
                        <button type="button" class="btn-secondary" id="btnGenRetryAllFailed">
                            <i class="fa-solid fa-rotate-right"></i> Generate ulang ${failedIndexes.length} scene gagal
                        </button>
                    </div>
                    <div class="rp-file-list">
                        ${genSceneResults.map((s, i) => `
                            <div class="rp-file-item">
                                <div class="rp-file-icon">${s.status === 'success' ? '<i class="fa-solid fa-check"></i>' : '<i class="fa-solid fa-triangle-exclamation"></i>'}</div>
                                <div class="rp-file-main">
                                    <div class="rp-file-name">${escapeHtml(s.title)}</div>
                                    <div class="rp-file-path">${escapeHtml(s.status === 'success' ? s.videoPath : s.error)}</div>
                                </div>
                                ${s.status === 'success' ? '' : `
                                    <div class="rp-toolbar-actions">
                                        <button type="button" class="btn-secondary gen-retry-scene" data-idx="${i}" title="Generate ulang scene video ini">
                                            <i class="fa-solid fa-rotate-right"></i> Retry
                                        </button>
                                    </div>
                                `}
                            </div>
                        `).join('')}
                    </div>
                `;
                panel.classList.remove('view-hidden');
                panel.querySelectorAll('.gen-retry-scene').forEach(btn => {
                    btn.onclick = () => retryGenScenes([parseInt(btn.dataset.idx, 10)]);
                });
                const retryAll = document.getElementById('btnGenRetryAllFailed');
                if (retryAll) retryAll.onclick = () => retryGenScenes(failedIndexes);
            }

            function updateGenFilesFromResults() {
                const ordered = genSceneResults.length
                    ? genSceneResults.map(s => s && s.videoPath).filter(Boolean)
                    : lastGenFiles;
                lastGenFiles = uniquePaths(ordered);
            }

            function handleGenResult(res, retryContext = null) {
                const files = (res.files || []).filter(f => /\.(mp4|mov|mkv|webm)$/i.test(f));
                if (retryContext && genSceneResults.length) {
                    (retryContext.indexes || []).forEach((originalIdx, localIdx) => {
                        const row = (res.rows || [])[localIdx];
                        const file = firstRowFile(row, /\.(mp4|mov|mkv|webm)$/i) || files[localIdx] || '';
                        const current = genSceneResults[originalIdx] || {
                            index: originalIdx,
                            title: ((lastGenPayload && lastGenPayload.scene_titles) || [])[originalIdx] || `Scene ${originalIdx + 1}`,
                        };
                        genSceneResults[originalIdx] = {
                            ...current,
                            videoPath: (isSuccessRow(row) && file) ? file : '',
                            status: (isSuccessRow(row) && file) ? 'success' : 'failed',
                            error: (isSuccessRow(row) && file) ? '' : ((row && (row.error || row.status)) || res.error || 'Generate ulang scene video gagal.'),
                        };
                    });
                    updateGenFilesFromResults();
                } else {
                    genSceneResults = buildGenSceneResults(files, res.rows || []);
                    lastGenFiles = genSceneResults.length ? uniquePaths(genSceneResults.map(s => s.videoPath).filter(Boolean)) : files;
                }
                renderGenRetryPanel();
            }

            function retryGenScenes(indexes) {
                indexes = uniquePaths(indexes).map(i => parseInt(i, 10)).filter(i => Number.isFinite(i));
                if (!indexes.length) return;
                const selectedProfiles = Array.from(genProfilesList.querySelectorAll('.gen-profile-checkbox:checked')).map(c => c.value);
                if (!selectedProfiles.length) { showToast('Pilih akun pengganti dulu di Video Generator.', 'error'); return; }
                const base = clonePayload(lastGenPayload);
                const promptVariants = indexes.map(i =>
                    (base.prompt_variants || [])[i]
                    || base.prompt
                    || document.getElementById('genPromptText').value.trim()
                );
                const imageVariants = indexes.map(i =>
                    (base.image_variants || [])[i]
                    || genSceneImages[i]
                    || base.image_path
                    || document.getElementById('genImagePath').value.trim()
                );
                if (promptVariants.some(p => !p) || imageVariants.some(p => !p)) {
                    showToast('Data prompt/foto scene video gagal tidak lengkap.', 'error');
                    return;
                }
                const concurrency = Math.max(1, parseInt(document.getElementById('genConcurrency').value, 10) || 1);
                const payload = {
                    ...base,
                    profiles: selectedProfiles,
                    image_path: imageVariants[0],
                    image_variants: imageVariants,
                    prompt: promptVariants[0],
                    prompt_variants: promptVariants,
                    scene_titles: indexes.map(i => (base.scene_titles || [])[i] || `Scene ${i + 1}`),
                    output_names: indexes.map(i => (base.output_names || [])[i] || `scene_${String(i + 1).padStart(2, '0')}_retry`),
                    ratio: document.getElementById('genRatio').value,
                    duration: base.duration || document.getElementById('genDuration').value,
                    model: document.getElementById('genAgent').value,
                    runs: indexes.length,
                    concurrency: Math.min(concurrency, selectedProfiles.length, indexes.length),
                    hidden: document.getElementById('genHidden').checked,
                    mode: 'single',
                };
                currentGenRetryContext = { indexes };
                btnGenStart.disabled = true;
                const storyBtn = document.getElementById('btnImgGenStoryboardToVideo');
                if (storyBtn) storyBtn.disabled = true;
                btnGenStop.classList.remove('view-hidden');
                revealProgressPanel(genProgressPanel);
                showToast(`Generate ulang ${indexes.length} scene video dimulai.`, 'info');
                api('/api/generate/video', 'POST', payload)
                    .then(res => {
                        if (res.success && res.job_id) {
                            currentGenJobId = res.job_id;
                            pollGenJob(res.job_id);
                        } else {
                            currentGenRetryContext = null;
                            btnGenStart.disabled = false;
                            if (storyBtn) storyBtn.disabled = false;
                            btnGenStop.classList.add('view-hidden');
                            showToast(res.error || 'Gagal memulai retry video', 'error');
                        }
                    })
                    .catch(() => {
                        currentGenRetryContext = null;
                        btnGenStart.disabled = false;
                        if (storyBtn) storyBtn.disabled = false;
                        btnGenStop.classList.add('view-hidden');
                        showToast('Gagal terhubung ke server', 'error');
                    });
            }

            document.getElementById('btnImgGenStoryboardToVideo').onclick = () => {
                const scenes = (lastImgStoryboardScenes || []).filter(s => s.imagePath && s.videoPrompt);
                if (scenes.length < 3) {
                    showToast('Storyboard butuh minimal 3 foto scene yang berhasil.', 'error');
                    return;
                }
                const selectedProfiles = Array.from(genProfilesList.querySelectorAll('.gen-profile-checkbox:checked')).map(c => c.value);
                if (!selectedProfiles.length) {
                    showToast('Pilih minimal satu Google Profile di Video Generator', 'error');
                    return;
                }
                const genParallel = Math.max(1, parseInt(document.getElementById('genConcurrency').value, 10) || 1);
                const first = scenes[0];
                setGenSceneImages(scenes.map(s => s.imagePath), true);
                document.getElementById('genImagePath').value = first.imagePath;
                document.getElementById('genPromptText').value = first.videoPrompt;
                document.getElementById('genDuration').value = first.duration || document.getElementById('genDuration').value;
                document.getElementById('genRatio').value = '9:16';
                document.getElementById('genStoryboardName').value = document.getElementById('imgGenStoryboardName').value;
                document.getElementById('genCtaType').value = document.getElementById('imgGenCtaType').value;
                document.getElementById('genCustomCta').value = document.getElementById('imgGenCustomCta').value;
                document.getElementById('genCtaType').dispatchEvent(new Event('change'));
                const vbtn = document.querySelector('#generateSubNav .sub-tab-btn[data-sub="generator"]');
                if (vbtn) vbtn.click();

                const payload = {
                    profiles: selectedProfiles,
                    image_path: first.imagePath,
                    image_variants: scenes.map(s => s.imagePath),
                    prompt: first.videoPrompt,
                    prompt_variants: scenes.map(s => s.videoPrompt),
                    scene_titles: scenes.map(s => s.title),
                    output_names: scenes.map(s => s.outputName),
                    ratio: document.getElementById('genRatio').value,
                    duration: first.duration || document.getElementById('genDuration').value,
                    model: document.getElementById('genAgent').value,
                    runs: 1,
                    concurrency: Math.min(genParallel, selectedProfiles.length, scenes.length),
                    hidden: document.getElementById('genHidden').checked,
                    mode: 'single',
                };

                const storyBtn = document.getElementById('btnImgGenStoryboardToVideo');
                lastGenFiles = [];
                genSceneResults = [];
                lastGenPayload = clonePayload(payload);
                currentGenRetryContext = null;
                renderGenRetryPanel();
                btnGenStart.disabled = true;
                storyBtn.disabled = true;
                btnGenStop.classList.remove('view-hidden');
                revealProgressPanel(genProgressPanel);
                api('/api/generate/video', 'POST', payload)
                    .then(res => {
                        if (res.success && res.job_id) {
                            currentGenJobId = res.job_id;
                            pollGenJob(res.job_id);
                            showToast('Video storyboard mulai digenerate.', 'success');
                        } else {
                            btnGenStart.disabled = false;
                            storyBtn.disabled = false;
                            btnGenStop.classList.add('view-hidden');
                            showToast(res.error || 'Gagal memulai video storyboard', 'error');
                        }
                    })
                    .catch(() => {
                        btnGenStart.disabled = false;
                        storyBtn.disabled = false;
                        btnGenStop.classList.add('view-hidden');
                        showToast('Gagal terhubung ke server', 'error');
                    });
            };

            document.getElementById('btnGenOpenFolder').onclick = () => {
                // Prioritas: file hasil terakhir; fallback: folder gambar utama
                // (di situlah video/foto hasil disimpan).
                const target = (lastGenFiles && lastGenFiles[0]) || document.getElementById('genImagePath').value.trim();
                if (!target) { showToast('Belum ada gambar/hasil untuk dibuka', 'error'); return; }
                api('/api/reveal_folder', 'POST', { path: target }).then(d => {
                    if (!d.success) showToast(d.error || 'Gagal membuka folder', 'error');
                }).catch(() => showToast('Gagal terhubung ke server', 'error'));
            };

            document.getElementById('btnGenToMixer').onclick = () => {
                const videos = (lastGenFiles || []).filter(f => /\.(mp4|mov|mkv|webm)$/i.test(f || ''));
                if (!videos.length) {
                    showToast('Belum ada video hasil yang sukses. Generate video dulu.', 'error');
                    return;
                }
                initRepurposeView();
                if (typeof window.gamaSendVideosToMixer === 'function' && window.gamaSendVideosToMixer(videos)) return;
                showToast('Video Mixer belum siap', 'error');
            };

            btnGenStart.onclick = () => {
                const selectedProfiles = Array.from(genProfilesList.querySelectorAll('.gen-profile-checkbox:checked')).map(c => c.value);
                if (!selectedProfiles.length) {
                    showToast('Pilih minimal satu Google Profile', 'error');
                    return;
                }
                
                const videoSceneImages = getGenSceneImages();
                const imagePath = videoSceneImages[0] || document.getElementById('genImagePath').value.trim();
                if (!imagePath) {
                    showToast('Pilih file gambar utama', 'error');
                    return;
                }

                const genPrompt = document.getElementById('genPromptText').value.trim();
                if (!genPrompt) {
                    showToast('Isi teks prompt video dulu', 'error');
                    return;
                }

                const ratio = document.getElementById('genRatio').value;
                const duration = document.getElementById('genDuration').value;
                const model = document.getElementById('genAgent').value;
                const runs = document.getElementById('genRuns').value;
                const concurrency = Math.max(1, parseInt(document.getElementById('genConcurrency').value, 10) || 1);
                let videoScenes = [];
                if (videoSceneImages.length >= 3) {
                    videoScenes = buildStoryboardScenes(genPrompt, {
                        count: videoSceneImages.length,
                        duration,
                        style: document.getElementById('genApStyle').value,
                        platform: document.getElementById('genApPlatform').value,
                        note: 'Generate video multi-scene dari daftar foto scene.',
                        storyName: document.getElementById('genStoryboardName').value || document.getElementById('imgGenStoryboardName').value,
                        ctaType: document.getElementById('genCtaType').value,
                        customCta: document.getElementById('genCustomCta').value,
                    });
                }
                
                const payload = {
                    profiles: selectedProfiles,
                    image_path: imagePath,
                    image_variants: videoScenes.length ? videoSceneImages.slice(0, videoScenes.length) : [],
                    prompt: videoScenes.length ? videoScenes[0].videoPrompt : genPrompt,
                    prompt_variants: videoScenes.map(s => s.videoPrompt),
                    scene_titles: videoScenes.map(s => s.title),
                    output_names: videoScenes.map(s => s.outputName),
                    ratio: ratio,
                    duration: duration,
                    model: model,
                    runs: runs,
                    concurrency: videoScenes.length ? Math.min(concurrency, selectedProfiles.length, videoScenes.length) : concurrency,
                    hidden: document.getElementById('genHidden').checked,
                    mode: videoScenes.length ? 'single' : (selectedProfiles.length > 1 ? 'batch' : 'single')
                };

                lastGenFiles = [];
                genSceneResults = [];
                lastGenPayload = clonePayload(payload);
                currentGenRetryContext = null;
                renderGenRetryPanel();
                btnGenStart.disabled = true;
                btnGenStop.classList.remove('view-hidden');
                revealProgressPanel(genProgressPanel);
                
                api('/api/generate/video', 'POST', payload)
                    .then(res => {
                        if (res.success && res.job_id) {
                            currentGenJobId = res.job_id;
                            pollGenJob(res.job_id);
                        } else {
                            btnGenStart.disabled = false;
                            btnGenStop.classList.add('view-hidden');
                            showToast(res.error || 'Gagal memulai video generation', 'error');
                        }
                    })
                    .catch(() => {
                        btnGenStart.disabled = false;
                        btnGenStop.classList.add('view-hidden');
                        showToast('Gagal terhubung ke server', 'error');
                    });
            };
            
            function pollGenJob(jobId) {
                if (genPollTimer) clearTimeout(genPollTimer);
                
                api(`/api/youtube/job/${jobId}`, 'GET')
                    .then(res => {
                        genProgLabel.textContent = res.message || 'Memproses...';
                        const pct = res.pct || 0;
                        genProgPct.textContent = pct + '%';
                        genProgFill.style.width = pct + '%';
                        
                        let msg = res.message || '';
                        if (res.error) msg += '\nERROR: ' + res.error;
                        if (res.rows && res.rows.length) {
                            msg += '\n' + res.rows.map((r, i) => `${i+1}. ${r.title}: ${r.status}`).join('\n');
                        }
                        genProgMsg.textContent = msg;
                        
                        if (res.done) {
                            btnGenStart.disabled = false;
                            const storyBtn = document.getElementById('btnImgGenStoryboardToVideo');
                            if (storyBtn) storyBtn.disabled = false;
                            btnGenStop.classList.add('view-hidden');
                            const retryContext = currentGenRetryContext;
                            currentGenRetryContext = null;
                            handleGenResult(res, retryContext);
                            if (res.success) {
                                showToast('Generate video selesai!', 'success');
                            } else {
                                showToast(res.error || 'Generate video gagal', 'error');
                            }
                        } else {
                            genPollTimer = setTimeout(() => pollGenJob(jobId), 1500);
                        }
                    })
                    .catch(() => {
                        btnGenStart.disabled = false;
                        const storyBtn = document.getElementById('btnImgGenStoryboardToVideo');
                        if (storyBtn) storyBtn.disabled = false;
                        btnGenStop.classList.add('view-hidden');
                    });
            }
            
            btnGenStop.onclick = () => {
                if (!currentGenJobId) { showToast('Tidak ada proses yang berjalan', 'info'); return; }
                btnGenStop.disabled = true;
                api(`/api/youtube/job/${currentGenJobId}/stop`, 'POST')
                    .then(res => {
                        btnGenStop.disabled = false;
                        if (res.success) showToast('Proses generate dihentikan', 'info');
                        else showToast(res.error || 'Gagal menghentikan proses', 'error');
                    })
                    .catch(() => { btnGenStop.disabled = false; showToast('Gagal terhubung ke server', 'error'); });
            };

            const btnGenCheckCredits = document.getElementById('btnGenCheckCredits');
            btnGenCheckCredits.onclick = () => {
                const selectedProfiles = Array.from(genProfilesList.querySelectorAll('.gen-profile-checkbox:checked')).map(c => c.value);
                if (!selectedProfiles.length) {
                    showToast('Pilih minimal satu Google Profile', 'error');
                    return;
                }
                btnGenCheckCredits.disabled = true;
                revealProgressPanel(genProgressPanel);
                genProgLabel.textContent = 'Cek limit dimulai...';
                genProgPct.textContent = '0%';
                genProgFill.style.width = '0%';
                genProgMsg.textContent = '';

                api('/api/generate/check_credits', 'POST', {
                    profiles: selectedProfiles,
                    model: document.getElementById('genAgent').value
                }).then(res => {
                    if (res.success && res.job_id) {
                        currentGenJobId = res.job_id;
                        pollCreditJob(res.job_id);
                    } else {
                        btnGenCheckCredits.disabled = false;
                        showToast(res.error || 'Gagal memulai cek limit', 'error');
                    }
                }).catch(() => {
                    btnGenCheckCredits.disabled = false;
                    showToast('Gagal terhubung ke server', 'error');
                });
            };

            function pollCreditJob(jobId) {
                api(`/api/youtube/job/${jobId}`, 'GET')
                    .then(res => {
                        genProgLabel.textContent = res.message || 'Memeriksa...';
                        const pct = res.pct || 0;
                        genProgPct.textContent = pct + '%';
                        genProgFill.style.width = pct + '%';
                        let msg = '';
                        if (res.rows && res.rows.length) {
                            msg = res.rows.map((r, i) => `${i+1}. ${r.title}: ${r.status}`).join('\n');
                        }
                        genProgMsg.textContent = msg;

                        if (res.done) {
                            btnGenCheckCredits.disabled = false;
                            if (res.success) showToast('Cek limit selesai', 'success');
                            else showToast(res.error || 'Cek limit gagal', 'error');
                        } else {
                            setTimeout(() => pollCreditJob(jobId), 1500);
                        }
                    })
                    .catch(() => { btnGenCheckCredits.disabled = false; });
            }

            // Muat daftar profil untuk sub-tab default (Image Generator) saat
            // pertama kali dibuka, dan sediakan hook refresh untuk pembukaan
            // tab Generate berikutnya (profil bisa berubah / baru selesai dimuat).
            loadImageGeneratorProfiles();
            loadGeneratorProfiles();
            window.refreshGenerateProfiles = () => {
                loadImageGeneratorProfiles();
                loadGeneratorProfiles();
            };
        }

        // ---------- Prospek: Ekstraktor Google Maps + Cek WA ----------
        let prospekInitialized = false;
        let gmResults = [];
        let gmPollTimer = null;

        function initProspekView() {
            loadGmWaSessions();
            if (prospekInitialized) return;
            prospekInitialized = true;
            initProspekPhotoPanel();

            document.getElementById('btnGmScrape').onclick = () => {
                const keyword = document.getElementById('gmKeyword').value.trim();
                if (!keyword) { showToast('Isi kata kunci bisnis dulu', 'error'); return; }
                const payload = {
                    keyword,
                    location: document.getElementById('gmLocation').value.trim(),
                    limit: parseInt(document.getElementById('gmLimit').value, 10) || 20,
                };
                const btn = document.getElementById('btnGmScrape');
                btn.disabled = true;
                revealProgressPanel('gmProgressPanel');
                document.getElementById('gmResultWrap').classList.add('view-hidden');
                api('/api/gmaps/scrape', 'POST', payload).then(res => {
                    if (res.success && res.job_id) pollGmJob(res.job_id);
                    else { btn.disabled = false; showToast(res.error || 'Gagal memulai', 'error'); }
                }).catch(() => { btn.disabled = false; showToast('Gagal terhubung ke server', 'error'); });
            };

            document.getElementById('btnGmOpenFolder').onclick = () => {
                api('/api/gmaps/open_folder', 'POST', {}).then(d => {
                    if (!d.success) showToast(d.error || 'Gagal buka folder', 'error');
                }).catch(() => showToast('Gagal terhubung ke server', 'error'));
            };

            document.getElementById('btnGmCheckWa').onclick = () => {
                const session = document.getElementById('gmWaSession').value;
                if (!session) { showToast('Pilih sesi WhatsApp yang login dulu', 'error'); return; }
                const phones = gmResults.map(r => r.phone_wa).filter(Boolean);
                if (!phones.length) { showToast('Tidak ada nomor telepon untuk dicek', 'error'); return; }
                const btn = document.getElementById('btnGmCheckWa');
                btn.disabled = true;
                showToast(`Cek ${phones.length} nomor di WhatsApp...`, 'info');
                api('/api/gmaps/check_wa', 'POST', { session, phones }).then(d => {
                    btn.disabled = false;
                    if (!d.success) { showToast(d.error || 'Gagal cek WA', 'error'); return; }
                    const map = {};
                    (d.results || []).forEach(r => { map[r.phone] = r.on_wa; });
                    gmResults.forEach(r => { if (r.phone_wa in map) r._wa = map[r.phone_wa]; });
                    renderGmTable();
                    const n = (d.results || []).filter(r => r.on_wa).length;
                    showToast(`${n}/${phones.length} nomor punya WhatsApp aktif`, 'success');
                    // Tampilkan panel kirim kalau ada kontak WA aktif
                    const active = gmResults.filter(r => r._wa === true);
                    const panel = document.getElementById('gmReviewPanel');
                    panel.classList.toggle('view-hidden', !active.length);
                    updateGmTargetInfo();
                }).catch(() => { btn.disabled = false; showToast('Gagal terhubung ke server', 'error'); });
            };

            document.getElementById('gmTargetFilter').onchange = updateGmTargetInfo;

            document.getElementById('btnGmSendReview').onclick = () => {
                const session = document.getElementById('gmWaSession').value;
                if (!session) { showToast('Pilih sesi WhatsApp dulu', 'error'); return; }
                const message = document.getElementById('gmReviewMsg').value.trim();
                if (!message) { showToast('Tulis pesan dulu', 'error'); return; }
                const contacts = gmTargetContacts();
                if (!contacts.length) { showToast('Tidak ada kontak sesuai target (cek WA dulu)', 'error'); return; }
                const btn = document.getElementById('btnGmSendReview');
                btn.disabled = true;
                revealProgressPanel('gmSendProgress');
                api('/api/gmaps/send_review_request', 'POST', {
                    session, contacts, message,
                    link: document.getElementById('gmReviewLink').value.trim(),
                    media_path: document.getElementById('gmReviewMedia').value.trim(),
                }).then(res => {
                    if (res.success && res.job_id) pollGmSendJob(res.job_id);
                    else { btn.disabled = false; showToast(res.error || 'Gagal memulai', 'error'); }
                }).catch(() => { btn.disabled = false; showToast('Gagal terhubung ke server', 'error'); });
            };
        }

        function pollGmSendJob(jobId) {
            api(`/api/youtube/job/${jobId}`, 'GET').then(res => {
                document.getElementById('gmSendLabel').textContent = res.message || 'Memproses...';
                const pct = res.pct || 0;
                document.getElementById('gmSendPct').textContent = pct + '%';
                document.getElementById('gmSendFill').style.width = pct + '%';
                let msg = '';
                if (res.rows && res.rows.length) msg = res.rows.map((r, i) => `${i + 1}. ${r.title}: ${r.status}`).join('\n');
                document.getElementById('gmSendMsg').textContent = msg;
                if (res.done) {
                    document.getElementById('btnGmSendReview').disabled = false;
                    if (res.success) showToast('Ajakan review terkirim!', 'success');
                    else showToast(res.error || 'Pengiriman gagal', 'error');
                } else {
                    setTimeout(() => pollGmSendJob(jobId), 1500);
                }
            }).catch(() => { document.getElementById('btnGmSendReview').disabled = false; });
        }

        function loadGmWaSessions() {
            const sel = document.getElementById('gmWaSession');
            if (!sel) return;
            fetch('/api/wa/status').then(r => r.json()).then(d => {
                sel.innerHTML = '';
                const conn = (d.sessions || []).filter(s => (s.status || '') === 'connected');
                if (!conn.length) { sel.add(new Option('(tak ada sesi WA login)', '')); return; }
                conn.forEach(s => sel.add(new Option(s.name, s.name)));
            }).catch(() => { sel.innerHTML = ''; sel.add(new Option('(gagal muat sesi WA)', '')); });
        }

        function pollGmJob(jobId) {
            if (gmPollTimer) clearTimeout(gmPollTimer);
            api(`/api/youtube/job/${jobId}`, 'GET').then(res => {
                document.getElementById('gmProgLabel').textContent = res.message || 'Memproses...';
                const pct = res.pct || 0;
                document.getElementById('gmProgPct').textContent = pct + '%';
                document.getElementById('gmProgFill').style.width = pct + '%';
                document.getElementById('gmProgMsg').textContent = res.error || '';
                if (res.results) { gmResults = res.results; renderGmTable(); }
                if (res.done) {
                    document.getElementById('btnGmScrape').disabled = false;
                    if (res.success) {
                        showToast('Ekstraksi bisnis selesai!', 'success');
                        document.getElementById('gmResultWrap').classList.remove('view-hidden');
                    } else {
                        showToast(res.error || 'Ekstraksi gagal', 'error');
                    }
                } else {
                    gmPollTimer = setTimeout(() => pollGmJob(jobId), 1500);
                }
            }).catch(() => { document.getElementById('btnGmScrape').disabled = false; });
        }

        function renderGmTable() {
            const t = document.getElementById('gmTable');
            const info = document.getElementById('gmResultInfo');
            if (!gmResults.length) { t.innerHTML = ''; info.textContent = ''; return; }
            const withPhone = gmResults.filter(r => r.phone).length;
            const withWa = gmResults.filter(r => r._wa === true).length;
            info.textContent = `${gmResults.length} bisnis | ${withPhone} punya telepon` +
                (withWa ? ` | ${withWa} punya WA` : '');
            let html = '<tr><th>Nama</th><th>Kategori</th><th>Telepon</th><th>WA</th><th>Alamat</th><th>Website</th><th>Rating</th></tr>';
            gmResults.forEach(r => {
                let wa = '<span class="sb-muted">?</span>';
                if (r._wa === true) wa = '<span style="color:#25D366;font-weight:600;">OK WA</span>';
                else if (r._wa === false) wa = '<span class="sb-muted"></span>';
                const web = r.website ? `<a href="${escapeHtml(r.website)}" class="chat-link" target="_blank" rel="noopener">link</a>` : '';
                html += `<tr>
                    <td>${escapeHtml(r.name || '')}</td>
                    <td>${escapeHtml(r.category || '')}</td>
                    <td>${escapeHtml(r.phone || '')}</td>
                    <td>${wa}</td>
                    <td title="${escapeHtml(r.address || '')}">${escapeHtml((r.address || '').slice(0, 40))}</td>
                    <td>${web}</td>
                    <td>${escapeHtml(r.rating || '')}</td>
                </tr>`;
            });
            t.innerHTML = html;
        }

        // ---------- Prospek: Foto Ulasan buat Konten TikTok ----------
        let gmPhotoPollTimer = null;
        let gmLastPhotoFolder = '';
        let gmLastPhotos = [];
        let gmLastPlaceName = '';
        let gmSelectedPhotoPaths = [];

        function initProspekPhotoPanel() {
            document.getElementById('btnGmPhotoDownload').onclick = () => {
                const place = document.getElementById('gmPhotoPlace').value.trim();
                if (!place) { showToast('Isi nama tempat atau link Google Maps dulu', 'error'); return; }
                const limit = parseInt(document.getElementById('gmPhotoCount').value, 10) || 20;
                const filters = {
                    sort_best_quality: document.getElementById('gmPhotoBestQuality').checked,
                    require_people: document.getElementById('gmPhotoPeople').checked,
                    require_detail: document.getElementById('gmPhotoDetail').checked,
                };
                const btn = document.getElementById('btnGmPhotoDownload');
                btn.disabled = true;
                gmSelectedPhotoPaths = [];
                document.getElementById('gmPhotoThumbs').innerHTML = '';
                document.getElementById('gmPhotoSelectInfo').textContent = 'Klik foto untuk memilih 3 foto terbaik sebelum kirim ke storyboard.';
                revealProgressPanel('gmPhotoProgress');
                api('/api/gmaps/scrape_photos', 'POST', { place, limit, ...filters }).then(res => {
                    if (res.success && res.job_id) pollGmPhotoJob(res.job_id);
                    else { btn.disabled = false; showToast(res.error || 'Gagal memulai', 'error'); }
                }).catch(() => { btn.disabled = false; showToast('Gagal terhubung ke server', 'error'); });
            };

            document.getElementById('btnGmPhotoOpenFolder').onclick = () => {
                if (!gmLastPhotoFolder) { showToast('Belum ada foto yang diunduh', 'error'); return; }
                api('/api/reveal_folder', 'POST', { path: gmLastPhotoFolder }).then(d => {
                    if (!d.success) showToast(d.error || 'Gagal buka folder', 'error');
                }).catch(() => showToast('Gagal terhubung ke server', 'error'));
            };

            document.getElementById('btnGmPhotoToStoryboard3').onclick = () => {
                const paths = gmLastPhotos
                    .filter(p => gmSelectedPhotoPaths.includes(p.path))
                    .map(p => p.path)
                    .filter(Boolean);
                if (paths.length !== 3) {
                    showToast(`Pilih tepat 3 foto dulu. Saat ini terpilih ${paths.length}.`, 'error');
                    return;
                }
                useReviewPhotosInStoryboard(paths);
            };

            document.getElementById('btnGmPhotoToStoryboardAll').onclick = () => {
                const paths = gmLastPhotos.map(p => p.path).filter(Boolean);
                useReviewPhotosInStoryboard(paths);
            };
        }

        function pollGmPhotoJob(jobId) {
            if (gmPhotoPollTimer) clearTimeout(gmPhotoPollTimer);
            api(`/api/youtube/job/${jobId}`, 'GET').then(res => {
                document.getElementById('gmPhotoLabel').textContent = res.message || 'Memproses...';
                const pct = res.pct || 0;
                document.getElementById('gmPhotoPct').textContent = pct + '%';
                document.getElementById('gmPhotoFill').style.width = pct + '%';
                document.getElementById('gmPhotoMsg').textContent = res.error || '';
                if (res.place_name) gmLastPlaceName = res.place_name;
                if (res.photos) {
                    gmLastPhotos = res.photos || [];
                    renderGmPhotoThumbs(gmLastPhotos);
                }
                if (res.folder) gmLastPhotoFolder = res.folder;
                if (res.done) {
                    document.getElementById('btnGmPhotoDownload').disabled = false;
                    if (res.success) showToast('Foto ulasan selesai diunduh!', 'success');
                    else showToast(res.error || 'Gagal mengambil foto ulasan', 'error');
                } else {
                    gmPhotoPollTimer = setTimeout(() => pollGmPhotoJob(jobId), 1500);
                }
            }).catch(() => { document.getElementById('btnGmPhotoDownload').disabled = false; });
        }

        function renderGmPhotoThumbs(photos) {
            const wrap = document.getElementById('gmPhotoThumbs');
            const currentPaths = (photos || []).map(p => p.path).filter(Boolean);
            gmSelectedPhotoPaths = gmSelectedPhotoPaths.filter(p => currentPaths.includes(p));
            const selectedSet = new Set(gmSelectedPhotoPaths);
            document.getElementById('gmPhotoSelectInfo').textContent =
                `${(photos || []).length} foto tampil. ${gmSelectedPhotoPaths.length}/3 foto dipilih untuk storyboard.`;
            wrap.innerHTML = (photos || []).map((p, i) => {
                const meta = [
                    `Foto ${i + 1}`,
                    p.reviewer || '',
                    (p.width && p.height) ? `${p.width}x${p.height}` : '',
                    p.quality_score ? `quality ${p.quality_score}` : '',
                    p.has_people ? 'ada manusia' : '',
                    p.detail_score ? `detail ${p.detail_score}` : '',
                    'klik untuk pilih storyboard'
                ].filter(Boolean).join(' - ');
                const selected = selectedSet.has(p.path);
                return `
                <button type="button" class="gm-photo-pick" data-idx="${i}"
                        style="position:relative;border:${selected ? '3px solid #8b6cff' : '1px solid rgba(139,108,255,.18)'};padding:0;border-radius:8px;background:transparent;cursor:pointer;overflow:hidden;box-shadow:${selected ? '0 0 0 3px rgba(139,108,255,.18)' : 'none'};"
                        title="${escapeHtml(meta)}">
                    <img src="/api/preview_image?path=${encodeURIComponent(p.path)}" alt="foto ulasan"
                         class="img-result-thumb" style="display:block;margin:0;">
                    <span style="position:absolute;left:6px;top:6px;background:${selected ? '#8b6cff' : 'rgba(20,18,35,.72)'};color:white;border-radius:999px;font-size:0.72rem;font-weight:700;line-height:1;padding:0.32rem 0.45rem;">${selected ? (gmSelectedPhotoPaths.indexOf(p.path) + 1) : (i + 1)}</span>
                    ${selected ? '<span style="position:absolute;right:6px;top:6px;background:#fff;color:#6f55e8;border-radius:999px;font-size:0.72rem;font-weight:800;line-height:1;padding:0.32rem 0.45rem;">PILIH</span>' : ''}
                </button>
            `;
            }).join('');
            wrap.querySelectorAll('.gm-photo-pick').forEach(btn => {
                btn.onclick = () => {
                    const p = (photos[parseInt(btn.dataset.idx, 10)] || {}).path;
                    if (!p) return;
                    if (gmSelectedPhotoPaths.includes(p)) {
                        gmSelectedPhotoPaths = gmSelectedPhotoPaths.filter(x => x !== p);
                    } else {
                        if (gmSelectedPhotoPaths.length >= 3) {
                            showToast('Maksimal 3 foto untuk tombol Kirim 3 Foto. Hapus pilihan lain dulu.', 'info');
                            return;
                        }
                        gmSelectedPhotoPaths.push(p);
                    }
                    renderGmPhotoThumbs(photos);
                };
                btn.ondblclick = () => useReviewPhotoInGenerator((photos[parseInt(btn.dataset.idx, 10)] || {}).path);
            });
        }

        // Kirim foto terpilih ke Image Generator (isi field + pindah tab).
        // Tidak manggil requestPrompt() punya Generator langsung karena itu
        // fungsi privat di closure initGeneratorView() - cukup isi field lalu
        // pindah tab. Niche TikTok Go dipilih di dropdown gaya Image Generator.
        function useReviewPhotoInGenerator(path) {
            if (!path) return;
            document.getElementById('imgGenApPlatform').value = 'tiktok';
            document.getElementById('imgGenCtaType').value = 'tiktok_go';
            document.getElementById('imgGenCtaType').dispatchEvent(new Event('change'));
            document.getElementById('imgGenStoryboardName').value = gmLastPlaceName || String((path || '').split(/[\\/]/).slice(-2, -1)[0] || '').replace(/\s+/g, ' ').trim();
            switchView('generate');
            const btn = document.querySelector('#generateSubNav .sub-tab-btn[data-sub="scraper"]');
            if (btn) btn.click();
            if (typeof window.gamaSetImageGeneratorReferences === 'function') {
                window.gamaSetImageGeneratorReferences([path], { replace: true, storyboard: false });
            } else {
                document.getElementById('imgGenImagePath').value = path;
            }
            showToast('Foto dipakai di Image Generator - pilih gaya TikTok Go lalu klik "Auto Prompt".', 'success');
        }

        function useReviewPhotosInStoryboard(paths) {
            paths = (paths || []).filter(Boolean);
            if (paths.length < 3) {
                showToast('Storyboard butuh minimal 3 foto dari hasil scrape.', 'error');
                return;
            }
            switchView('generate');
            const btn = document.querySelector('#generateSubNav .sub-tab-btn[data-sub="scraper"]');
            if (btn) btn.click();
            document.getElementById('imgGenApPlatform').value = 'tiktok';
            document.getElementById('imgGenStoryboardMode').checked = true;
            document.getElementById('imgGenCtaType').value = 'tiktok_go';
            document.getElementById('imgGenCtaType').dispatchEvent(new Event('change'));
            document.getElementById('imgGenStoryboardName').value = gmLastPlaceName || document.getElementById('gmPhotoPlace').value.trim();
            document.getElementById('genStoryboardName').value = document.getElementById('imgGenStoryboardName').value;
            document.getElementById('imgGenSceneCount').value = paths.length;
            document.getElementById('imgGenRuns').value = paths.length;
            document.getElementById('imgGenRatio').value = '9:16';
            if (typeof window.gamaSetImageGeneratorReferences === 'function') {
                window.gamaSetImageGeneratorReferences(paths, { replace: true, storyboard: true });
            } else {
                document.getElementById('imgGenImagePath').value = paths[0];
            }
            showToast(`${paths.length} foto dikirim ke Storyboard Image Generator. 1 scene = 1 foto.`, 'success');
        }

        // Kontak sesuai segmen target terpilih (WA aktif + filter website).
        function gmTargetContacts() {
            const mode = (document.getElementById('gmTargetFilter') || {}).value || 'active';
            return gmResults.filter(r => {
                if (r._wa !== true) return false;
                const hasWeb = !!(r.website && r.website.trim());
                if (mode === 'nowebsite') return !hasWeb;
                if (mode === 'haswebsite') return hasWeb;
                return true;
            }).map(r => ({ phone: r.phone_wa, name: r.name || '' }));
        }

        function updateGmTargetInfo() {
            const info = document.getElementById('gmReviewInfo');
            if (!info) return;
            const active = gmResults.filter(r => r._wa === true);
            const noWeb = active.filter(r => !(r.website && r.website.trim())).length;
            const n = gmTargetContacts().length;
            info.textContent = `${n} kontak sesuai target akan dikirimi ` +
                `(total WA aktif: ${active.length}, tanpa website: ${noWeb})`;
        }

        // ---------- Context menu klik-kanan (Cut/Copy/Paste) ----------
        // pywebview/WebView2 sering tak menampilkan menu klik-kanan bawaan atau
        // memblokir clipboard web. Menu kustom ini memakai jembatan clipboard
        // native (window.pywebview.api) dengan fallback navigator.clipboard.
        (function initContextMenu() {
            const menu = document.getElementById('ctxMenu');
            if (!menu) return;
            let targetEl = null;

            const isEditable = (el) => el && (el.tagName === 'TEXTAREA' ||
                (el.tagName === 'INPUT' && !['checkbox', 'radio', 'range', 'color', 'file', 'button', 'submit'].includes((el.type || 'text').toLowerCase())));

            async function clipboardRead() {
                if (window.pywebview && window.pywebview.api && window.pywebview.api.clipboard_get) {
                    try { return await window.pywebview.api.clipboard_get(); } catch (e) {}
                }
                try { return await navigator.clipboard.readText(); } catch (e) { return ''; }
            }
            async function clipboardWrite(text) {
                if (window.pywebview && window.pywebview.api && window.pywebview.api.clipboard_set) {
                    try { await window.pywebview.api.clipboard_set(text); return; } catch (e) {}
                }
                try { await navigator.clipboard.writeText(text); } catch (e) {}
            }

            function hideMenu() { menu.classList.remove('open'); targetEl = null; }

            document.addEventListener('contextmenu', (e) => {
                const el = e.target;
                const editable = isEditable(el);
                const hasSelection = editable && el.selectionStart !== el.selectionEnd;
                // Untuk teks biasa (non-editable) yang diseleksi, tetap tampilkan Salin.
                const pageSelection = String(window.getSelection ? window.getSelection() : '').length > 0;
                if (!editable && !pageSelection) { hideMenu(); return; }  // biarkan default
                e.preventDefault();
                targetEl = editable ? el : null;
                // Aktif/nonaktif item sesuai konteks
                menu.querySelector('[data-act="cut"]').disabled = !hasSelection;
                menu.querySelector('[data-act="copy"]').disabled = !(hasSelection || pageSelection);
                menu.querySelector('[data-act="paste"]').disabled = !editable;
                menu.querySelector('[data-act="all"]').disabled = !editable;
                // Posisikan (jaga tetap di dalam layar)
                menu.style.visibility = 'hidden';
                menu.classList.add('open');
                const mw = menu.offsetWidth, mh = menu.offsetHeight;
                let x = e.clientX, y = e.clientY;
                if (x + mw > window.innerWidth) x = window.innerWidth - mw - 4;
                if (y + mh > window.innerHeight) y = window.innerHeight - mh - 4;
                menu.style.left = x + 'px';
                menu.style.top = y + 'px';
                menu.style.visibility = 'visible';
            });

            menu.addEventListener('mousedown', (e) => e.preventDefault()); // jangan hilangkan fokus/seleksi
            menu.addEventListener('click', async (e) => {
                const btn = e.target.closest('.ctx-item');
                if (!btn || btn.disabled) return;
                const act = btn.dataset.act;
                const el = targetEl;
                if (act === 'copy') {
                    let text = '';
                    if (el && el.selectionStart !== el.selectionEnd) text = el.value.substring(el.selectionStart, el.selectionEnd);
                    else text = String(window.getSelection());
                    if (text) await clipboardWrite(text);
                } else if (act === 'cut' && el) {
                    const s = el.selectionStart, en = el.selectionEnd;
                    if (s !== en) {
                        await clipboardWrite(el.value.substring(s, en));
                        el.setRangeText('', s, en, 'end');
                        el.dispatchEvent(new Event('input', { bubbles: true }));
                    }
                } else if (act === 'paste' && el) {
                    const text = await clipboardRead();
                    if (text != null) {
                        const s = el.selectionStart, en = el.selectionEnd;
                        el.setRangeText(text, s, en, 'end');
                        el.dispatchEvent(new Event('input', { bubbles: true }));
                    }
                } else if (act === 'all' && el) {
                    el.focus(); el.select();
                }
                hideMenu();
            });

            document.addEventListener('mousedown', (e) => { if (!menu.contains(e.target)) hideMenu(); });
            document.addEventListener('scroll', hideMenu, true);
            window.addEventListener('blur', hideMenu);
            document.addEventListener('keydown', (e) => { if (e.key === 'Escape') hideMenu(); });
        })();
        // --- FB Group Tools ------------------------------------------------------
        let fbGroupInitialized = false;
        let fbGroupScrapedGroups = [];

        function switchFbGroupTab(tab) {
            document.getElementById('fbGrpPanelPost').classList.toggle('view-hidden', tab !== 'post');
            document.getElementById('fbGrpPanelScrape').classList.toggle('view-hidden', tab !== 'scrape');
            document.getElementById('fbGrpPanelJoin').classList.toggle('view-hidden', tab !== 'join');
            document.getElementById('fbGrpTabPost').classList.toggle('active', tab === 'post');
            document.getElementById('fbGrpTabScrape').classList.toggle('active', tab === 'scrape');
            document.getElementById('fbGrpTabJoin').classList.toggle('active', tab === 'join');
        }

        // Isi dropdown profil dengan akun Facebook - pakai mekanisme yang sama
        // dengan dropdown akun di tab Uploader (grup GAMA yang cocok platform facebook).
        function fillFbGroupProfiles() {
            const { profiles } = profilesForPlatform('facebook');
            const allIds = ['fbGrpPostProfile', 'fbGrpScrapeProfile', 'fbGrpJoinProfile'];
            allIds.forEach(id => {
                const sel = document.getElementById(id);
                if (!sel) return;
                const cur = sel.value;
                sel.innerHTML = '<option value="">Pilih akun...</option>';
                profiles.forEach(p => {
                    sel.add(new Option(getUploadAccountLabel(p, 'facebook'), p.name));
                });
                if (cur && profiles.some(p => p.name === cur)) sel.value = cur;
            });
        }

        function initFbGroupView() {
            if (fbGroupInitialized) return;
            fbGroupInitialized = true;

            document.getElementById('fbGrpTabPost').addEventListener('click', () => switchFbGroupTab('post'));
            document.getElementById('fbGrpTabScrape').addEventListener('click', () => switchFbGroupTab('scrape'));
            document.getElementById('fbGrpTabJoin').addEventListener('click', () => switchFbGroupTab('join'));

            // Render checklist grup di panel Post ke Grup (bisa ceklis lebih dari satu)
            function renderPostGroupList(groups) {
                const container = document.getElementById('fbGrpPostList');
                if (!groups || !groups.length) {
                    container.innerHTML = '<p class="sb-muted">Belum ada grup - klik Deteksi untuk memuat grup yang sudah di-join.</p>';
                    return;
                }
                container.innerHTML = '';
                groups.forEach(g => {
                    const row = document.createElement('div');
                    row.className = 'fb-grp-row';
                    row.innerHTML = `
                        <input type="checkbox" class="fb-grp-post-chk" data-id="${g.id}" data-name="${escapeHtml(g.name)}">
                        <span class="fb-grp-row-name">${escapeHtml(g.name)}</span>
                        <span class="fb-grp-privacy is-unknown" data-id="${g.id}">?</span>
                        <small class="fb-grp-hint">${g.id}</small>
                    `;
                    container.appendChild(row);
                });
            }

            // Isi checklist grup yang sudah di-cache saat profil post berubah
            document.getElementById('fbGrpPostProfile').addEventListener('change', () => {
                const profile = document.getElementById('fbGrpPostProfile').value;
                if (!profile) { renderPostGroupList([]); return; }
                const p = allProfiles.find(x => x.name === profile);
                renderPostGroupList((p && p.fb_groups) || []);
            });

            // Pilih Semua (checklist Post ke Grup)
            document.getElementById('btnPostSelectAll').addEventListener('click', () => {
                const all = document.querySelectorAll('.fb-grp-post-chk');
                const allChecked = Array.from(all).every(c => c.checked);
                all.forEach(c => c.checked = !allChecked);
            });

            // Cek privasi (privat/publik) grup yang tercentang, sebelum posting
            document.getElementById('btnCheckPostPrivacy').addEventListener('click', () => {
                const profile = document.getElementById('fbGrpPostProfile').value;
                if (!profile) { showToast('Pilih akun Facebook dulu', 'error'); return; }
                const checked = Array.from(document.querySelectorAll('.fb-grp-post-chk:checked')).map(c => c.dataset.id);
                const ids = checked.length ? checked : Array.from(document.querySelectorAll('.fb-grp-post-chk')).map(c => c.dataset.id);
                if (!ids.length) { showToast('Belum ada grup untuk dicek - klik Deteksi dulu', 'error'); return; }
                const btn = document.getElementById('btnCheckPostPrivacy');
                btn.disabled = true;
                ids.forEach(id => {
                    const badge = document.querySelector(`.fb-grp-privacy[data-id="${id}"]`);
                    if (badge) { badge.className = 'fb-grp-privacy is-checking'; badge.textContent = '...'; }
                });
                api('/api/facebook/groups/privacy', 'POST', { profile, group_ids: ids }).then(d => {
                    if (!d.job_id) { showToast(d.error || 'Gagal memulai cek privasi', 'error'); btn.disabled = false; return; }
                    const poll = setInterval(() => {
                        fetch(`/api/youtube/job/${d.job_id}`).then(r => r.json()).then(s => {
                            (s.results || []).forEach(r => {
                                const badge = document.querySelector(`.fb-grp-privacy[data-id="${r.id}"]`);
                                if (!badge) return;
                                const label = r.privacy === 'private' ? 'Privat' : r.privacy === 'public' ? 'Publik' : '?';
                                badge.className = `fb-grp-privacy is-${r.privacy || 'unknown'}`;
                                badge.textContent = label;
                            });
                            if (!s.done) return;
                            clearInterval(poll);
                            btn.disabled = false;
                            showToast(s.message || (s.success ? 'Cek privasi selesai' : s.error || 'Gagal'), s.success ? 'success' : 'error');
                        }).catch(() => { clearInterval(poll); btn.disabled = false; });
                    }, 1500);
                }).catch(() => { showToast('Gagal terhubung ke server', 'error'); btn.disabled = false; });
            });

            // Tombol Deteksi grup di panel Post ke Grup - scrape grup yang sudah
            // di-join akun terpilih lalu isi checklist + simpan ke cache.
            document.getElementById('btnDetectPostGroups').addEventListener('click', () => {
                const profile = document.getElementById('fbGrpPostProfile').value;
                if (!profile) { showToast('Pilih akun Facebook dulu', 'error'); return; }
                const short = profile.replace('_gmail.com', '');
                const btn = document.getElementById('btnDetectPostGroups');
                showToast(`Mendeteksi grup ${short}... (bisa 20-40 detik)`, 'info');
                btn.disabled = true;
                fetch('/api/facebook/groups', {method:'POST', headers:{'Content-Type':'application/json'},
                    body: JSON.stringify({profile, cached_only: false})})
                    .then(r => r.json()).then(d => {
                        if (!d.job_id) { showToast(d.error || 'Gagal memulai deteksi grup', 'error'); btn.disabled = false; return; }
                        const poll = setInterval(() => {
                            fetch(`/api/youtube/job/${d.job_id}`).then(r => r.json()).then(s => {
                                if (!s.done) return;
                                clearInterval(poll);
                                btn.disabled = false;
                                const groups = s.groups || [];
                                showToast(`${short}: ${groups.length} grup ditemukan`, groups.length ? 'success' : 'info');
                                if (groups.length) {
                                    fetch('/api/facebook/groups/save', {method:'POST', headers:{'Content-Type':'application/json'},
                                        body: JSON.stringify({profile, groups})});
                                    renderPostGroupList(groups);
                                    const p = allProfiles.find(x => x.name === profile);
                                    if (p) p.fb_groups = groups;
                                }
                            }).catch(() => { clearInterval(poll); btn.disabled = false; });
                        }, 2000);
                    }).catch(() => { showToast('Gagal terhubung ke server', 'error'); btn.disabled = false; });
            });

            fillFbGroupProfiles();

            // -- Handler: Post ke Grup ------------------------------------------
            function _pollProgress(jobId, opts) {
                // opts: { labelEl, pctEl, fillEl, msgEl, progEl, btnEl, onDone }
                revealProgressPanel(opts.progEl);
                opts.btnEl.disabled = true;
                const poll = setInterval(() => {
                    fetch(`/api/youtube/job/${jobId}`).then(r => r.json()).then(s => {
                        opts.pctEl.textContent = (s.pct || 0) + '%';
                        opts.fillEl.style.width = (s.pct || 0) + '%';
                        opts.msgEl.textContent = s.message || '';
                        if (!s.done) return;
                        clearInterval(poll);
                        opts.btnEl.disabled = false;
                        showToast(s.message || (s.success ? 'Selesai' : s.error || 'Gagal'), s.success ? 'success' : 'error');
                        if (opts.onDone) opts.onDone(s);
                    }).catch(() => { clearInterval(poll); opts.btnEl.disabled = false; });
                }, 2000);
            }

            document.getElementById('btnPostToGroup').addEventListener('click', () => {
                const profile = document.getElementById('fbGrpPostProfile').value;
                if (!profile) { showToast('Pilih akun Facebook dulu', 'error'); return; }

                const checked = Array.from(document.querySelectorAll('.fb-grp-post-chk:checked'))
                    .map(c => ({ id: c.dataset.id, name: c.dataset.name || '' }));
                const manualIds = document.getElementById('fbGrpPostGroupId').value
                    .split(/[\n,]+/).map(s => s.trim()).filter(Boolean);
                const seen = new Set(checked.map(g => g.id));
                manualIds.forEach(id => { if (!seen.has(id)) { seen.add(id); checked.push({ id, name: '' }); } });

                if (!checked.length) { showToast('Ceklis minimal 1 grup atau isi Group ID manual', 'error'); return; }
                const caption = document.getElementById('fbGrpPostCaption').value.trim();
                const video_path = document.getElementById('fbGrpPostVideo').value.trim();
                if (!caption && !video_path) { showToast('Isi caption atau pilih file media', 'error'); return; }
                const post_delay = parseInt(document.getElementById('fbGrpPostDelay').value) || 0;
                const invisible = document.getElementById('fbGrpPostInvisible').checked;

                api('/api/facebook/group/post', 'POST', {
                    profile, group_ids: checked,
                    caption, video_path: video_path || '',
                    post_delay, invisible,
                }).then(d => {
                    if (!d.job_id) { showToast(d.error || 'Gagal memulai post', 'error'); return; }
                    _pollProgress(d.job_id, {
                        labelEl: document.getElementById('fbGrpPostLabel'),
                        pctEl: document.getElementById('fbGrpPostPct'),
                        fillEl: document.getElementById('fbGrpPostFill'),
                        msgEl: document.getElementById('fbGrpPostMsg'),
                        progEl: document.getElementById('fbGrpPostProgress'),
                        btnEl: document.getElementById('btnPostToGroup'),
                    });
                }).catch(() => showToast('Gagal terhubung ke server', 'error'));
            });

            // -- Handler: Render list grup dari hasil scrape ---------------------
            function renderGroupList(groups) {
                const container = document.getElementById('fbGrpList');
                container.innerHTML = '';
                groups.forEach(g => {
                    const row = document.createElement('div');
                    row.className = 'fb-grp-row';
                    row.innerHTML = `
                        <input type="checkbox" class="fb-grp-chk" data-id="${g.id}" data-name="${escapeHtml(g.name)}" checked>
                        <span class="fb-grp-row-name">${escapeHtml(g.name)}</span>
                        <small class="fb-grp-hint">${g.id}</small>
                        <a href="https://www.facebook.com/groups/${g.id}" target="_blank" class="fb-grp-row-link">buka</a>
                    `;
                    container.appendChild(row);
                });
                document.getElementById('fbGrpScrapeCount').textContent = `${groups.length} grup ditemukan`;
                document.getElementById('fbGrpScrapeResult').classList.remove('view-hidden');
            }

            // Pilih Semua
            document.getElementById('btnSelectAllGroups').addEventListener('click', () => {
                const all = document.querySelectorAll('.fb-grp-chk');
                const allChecked = Array.from(all).every(c => c.checked);
                all.forEach(c => c.checked = !allChecked);
            });

            // Simpan ke cache
            document.getElementById('btnSaveGroups').addEventListener('click', () => {
                const profile = document.getElementById('fbGrpScrapeProfile').value;
                if (!profile) { showToast('Pilih akun dulu', 'error'); return; }
                api('/api/facebook/groups/save', 'POST', { profile, groups: fbGroupScrapedGroups })
                    .then(d => {
                        if (d.success) {
                            showToast(`${fbGroupScrapedGroups.length} grup disimpan`, 'success');
                            const p = allProfiles.find(x => x.name === profile);
                            if (p) p.fb_groups = fbGroupScrapedGroups;
                            // Refresh dropdown grup di panel Post
                            document.getElementById('fbGrpPostProfile').dispatchEvent(new Event('change'));
                        } else {
                            showToast(d.error || 'Gagal menyimpan grup', 'error');
                        }
                    }).catch(() => showToast('Gagal terhubung ke server', 'error'));
            });

            // Export hasil scrape (atau yang tercentang) ke file CSV
            document.getElementById('btnExportGroupsCsv').addEventListener('click', () => {
                if (!fbGroupScrapedGroups.length) { showToast('Belum ada hasil scrape untuk di-export', 'error'); return; }
                const selectedIds = new Set(Array.from(document.querySelectorAll('.fb-grp-chk:checked')).map(c => c.dataset.id));
                const picked = fbGroupScrapedGroups.filter(g => selectedIds.has(String(g.id)));
                const list = picked.length ? picked : fbGroupScrapedGroups;
                const esc = v => `"${String(v == null ? '' : v).replace(/"/g, '""')}"`;
                const lines = ['group_id;group_name', ...list.map(g => `${esc(g.id)};${esc(g.name)}`)];
                const blob = new Blob(['' + lines.join('\r\n')], { type: 'text/csv;charset=utf-8;' });
                const url = URL.createObjectURL(blob);
                const a = document.createElement('a');
                const profShort = (document.getElementById('fbGrpScrapeProfile').value || 'grup').replace('_gmail.com', '');
                a.href = url;
                a.download = `facebook_groups_${profShort}.csv`;
                document.body.appendChild(a);
                a.click();
                document.body.removeChild(a);
                URL.revokeObjectURL(url);
                showToast(`${list.length} grup di-export ke CSV`, 'success');
            });

            // Scrape Grup
            document.getElementById('btnScrapeGroups').addEventListener('click', () => {
                const profile = document.getElementById('fbGrpScrapeProfile').value;
                if (!profile) { showToast('Pilih akun Facebook dulu', 'error'); return; }
                const btn = document.getElementById('btnScrapeGroups');
                api('/api/facebook/groups', 'POST', {
                    profile,
                    keyword: document.getElementById('fbGrpKeyword').value.trim(),
                    max_results: parseInt(document.getElementById('fbGrpMaxResults').value) || 100,
                    headless: document.getElementById('fbGrpHeadless').checked,
                }).then(d => {
                    if (!d.job_id) { showToast(d.error || 'Gagal scrape', 'error'); return; }
                    _pollProgress(d.job_id, {
                        labelEl: document.getElementById('fbGrpScrapeLabel'),
                        pctEl: document.getElementById('fbGrpScrapePct'),
                        fillEl: document.getElementById('fbGrpScrapeFill'),
                        msgEl: document.getElementById('fbGrpScrapeMsg'),
                        progEl: document.getElementById('fbGrpScrapeProgress'),
                        btnEl: btn,
                        onDone: s => {
                            if (s.success && s.groups) {
                                fbGroupScrapedGroups = s.groups;
                                renderGroupList(s.groups);
                            }
                        }
                    });
                }).catch(() => showToast('Gagal terhubung ke server', 'error'));
            });

            // Join grup terpilih dari hasil scrape
            document.getElementById('btnJoinSelectedGroups').addEventListener('click', () => {
                const profile = document.getElementById('fbGrpScrapeProfile').value;
                if (!profile) { showToast('Pilih akun dulu', 'error'); return; }
                const selected = Array.from(document.querySelectorAll('.fb-grp-chk:checked'))
                    .map(c => ({ id: c.dataset.id, name: c.dataset.name }));
                if (!selected.length) { showToast('Pilih minimal 1 grup', 'error'); return; }
                switchFbGroupTab('join');
                _doJoinGroups(profile, selected, 10, false);
            });

            // Post ke grup terpilih dari hasil scrape
            document.getElementById('btnPostSelectedGroups').addEventListener('click', () => {
                const profile = document.getElementById('fbGrpScrapeProfile').value;
                if (!profile) { showToast('Pilih akun dulu', 'error'); return; }
                const selected = Array.from(document.querySelectorAll('.fb-grp-chk:checked'))
                    .map(c => ({ id: c.dataset.id, name: c.dataset.name }));
                if (!selected.length) { showToast('Pilih minimal 1 grup', 'error'); return; }
                // Pindah ke tab Post, isi akun + checklist grup terpilih (langsung tercentang)
                switchFbGroupTab('post');
                document.getElementById('fbGrpPostProfile').value = profile;
                renderPostGroupList(selected);
                document.querySelectorAll('.fb-grp-post-chk').forEach(c => c.checked = true);
                document.getElementById('fbGrpPostGroupId').value = '';
                showToast(`${selected.length} grup dipilih untuk posting`, 'success');
            });

            // Muat daftar Group ID dari file CSV (mis. hasil Export CSV di tab Scrape Grup)
            document.getElementById('btnLoadJoinCsv').addEventListener('click', () => {
                const csvPath = document.getElementById('fbGrpJoinCsv').value.trim();
                if (!csvPath) { showToast('Pilih file CSV dulu', 'error'); return; }
                api('/api/facebook/groups/csv_load', 'POST', { csv_path: csvPath }).then(d => {
                    if (d.error) { showToast(d.error, 'error'); return; }
                    const groups = d.groups || [];
                    if (!groups.length) { showToast('Tidak ada Group ID ditemukan di CSV (butuh kolom group_id/id)', 'error'); return; }
                    document.getElementById('fbGrpJoinIds').value = groups.map(g => g.id).join('\n');
                    showToast(`${groups.length} Group ID dimuat dari CSV`, 'success');
                }).catch(() => showToast('Gagal terhubung ke server', 'error'));
            });

            // -- Handler: Auto Join ---------------------------------------------
            document.getElementById('btnAutoJoinGroups').addEventListener('click', () => {
                const profile = document.getElementById('fbGrpJoinProfile').value;
                if (!profile) { showToast('Pilih akun Facebook dulu', 'error'); return; }
                const raw = document.getElementById('fbGrpJoinIds').value.trim();
                if (!raw) { showToast('Isi Group ID dulu', 'error'); return; }
                const ids = raw.split(/[\n,]+/).map(s => s.trim()).filter(Boolean);
                const group_ids = ids.map(id => ({ id, name: '' }));
                _doJoinGroups(
                    profile, group_ids,
                    parseInt(document.getElementById('fbGrpJoinDelay').value) || 10,
                    document.getElementById('fbGrpJoinInvisible').checked
                );
            });

            function _doJoinGroups(profile, group_ids, delay, invisible) {
                const btn = document.getElementById('btnAutoJoinGroups');
                api('/api/facebook/groups/join', 'POST', { profile, group_ids, delay, invisible })
                    .then(d => {
                        if (!d.job_id) { showToast(d.error || 'Gagal join', 'error'); return; }
                        _pollProgress(d.job_id, {
                            labelEl: document.getElementById('fbGrpJoinLabel'),
                            pctEl: document.getElementById('fbGrpJoinPct'),
                            fillEl: document.getElementById('fbGrpJoinFill'),
                            msgEl: document.getElementById('fbGrpJoinMsg'),
                            progEl: document.getElementById('fbGrpJoinProgress'),
                            btnEl: btn,
                        });
                    }).catch(() => showToast('Gagal terhubung ke server', 'error'));
            }
        }
        // --- End FB Group Tools ---------------------------------------------------

        // --- Tab Lisensi (verifikasi offline Ed25519, per-fitur) -------------------
        let licenseInitialized = false;

        function renderFeatureList() {
            const box = document.getElementById('licFeatureList');
            const names = Object.keys(licenseFeatures);
            if (!names.length) {
                box.innerHTML = '<p class="sb-muted">Belum ada data fitur.</p>';
                return;
            }
            box.innerHTML = names.map(name => {
                const f = licenseFeatures[name] || {};
                const badgeClass = f.unlocked ? 'is-valid' : 'is-invalid';
                const badgeIcon = f.unlocked ? 'fa-lock-open' : 'fa-lock';
                const badgeText = f.unlocked ? 'Aktif' : 'Terkunci';
                const expiry = f.unlocked && f.expires_at ? ` <small class="fb-grp-hint">s.d. ${escapeHtml(f.expires_at)}</small>` : '';
                return `
                    <div class="fb-grp-row">
                        <span class="fb-grp-row-name">${escapeHtml(f.label || name)}</span>
                        <span class="lic-badge ${badgeClass}"><i class="fa-solid ${badgeIcon}"></i> ${badgeText}</span>
                        ${expiry}
                    </div>
                `;
            }).join('');
        }

        function initLicenseView() {
            if (licenseInitialized) return;
            licenseInitialized = true;

            fetch('/api/license/status').then(r => r.json()).then(d => {
                document.getElementById('licMachineId').value = d.machine_id || '';
                licenseFeatures = d.features || {};
                renderFeatureList();
            }).catch(() => {
                document.getElementById('licFeatureList').innerHTML = '<p class="sb-muted">Gagal memuat status lisensi.</p>';
            });

            document.getElementById('btnCopyMachineId').addEventListener('click', async () => {
                const text = document.getElementById('licMachineId').value;
                if (!text) return;
                if (window.pywebview && window.pywebview.api && window.pywebview.api.clipboard_set) {
                    try { await window.pywebview.api.clipboard_set(text); showToast('Machine ID disalin', 'success'); return; } catch (e) {}
                }
                try { await navigator.clipboard.writeText(text); showToast('Machine ID disalin', 'success'); } catch (e) { showToast('Gagal menyalin', 'error'); }
            });

            document.getElementById('btnVerifyLicense').addEventListener('click', () => {
                const key = document.getElementById('licKeyInput').value.trim();
                if (!key) { showToast('Isi license key dulu', 'error'); return; }
                const btn = document.getElementById('btnVerifyLicense');
                btn.disabled = true;
                api('/api/license/verify', 'POST', { license_key: key }).then(d => {
                    const r = d.result || {};
                    licenseFeatures = d.features || licenseFeatures;
                    renderFeatureList();
                    document.getElementById('licVerifyMsg').textContent = r.message || '';
                    showToast(r.message || 'Selesai diverifikasi', r.valid ? 'success' : 'error');
                    if (r.valid) document.getElementById('licKeyInput').value = '';
                }).catch(() => showToast('Gagal terhubung ke server', 'error'))
                  .then(() => { btn.disabled = false; });
            });
        }

        // --- Tab Metadata Cleaner (diadopsi dari D:\TOOLS\METADATA CLEANER) --------
        let metadataInitialized = false;
        let mcQueue = []; // [{path, name, status}]
        let mcDevicePresets = {};

        function renderMcQueue() {
            const box = document.getElementById('mcQueueList');
            if (!mcQueue.length) {
                box.innerHTML = '<p class="sb-muted">Antrean kosong - tambah file atau folder dulu.</p>';
                return;
            }
            box.innerHTML = mcQueue.map((f, i) => `
                <div class="fb-grp-row">
                    <span class="fb-grp-row-name">${escapeHtml(f.name)}</span>
                    <small class="fb-grp-hint">${escapeHtml(f.status || 'antre')}</small>
                    <button type="button" class="btn-secondary mc-remove-row" data-idx="${i}" style="padding:2px 8px;"><i class="fa-solid fa-xmark"></i></button>
                </div>
            `).join('');
            box.querySelectorAll('.mc-remove-row').forEach(btn => {
                btn.addEventListener('click', () => {
                    mcQueue.splice(parseInt(btn.dataset.idx), 1);
                    renderMcQueue();
                });
            });
        }

        function mcAddPaths(paths) {
            const existing = new Set(mcQueue.map(f => f.path));
            let added = 0;
            (paths || []).forEach(p => {
                if (!p || existing.has(p)) return;
                existing.add(p);
                mcQueue.push({ path: p, name: p.split(/[\\/]/).pop(), status: 'antre' });
                added++;
            });
            renderMcQueue();
            return added;
        }

        function initMetadataView() {
            if (metadataInitialized) return;
            metadataInitialized = true;

            fetch('/api/metadata/presets').then(r => r.json()).then(d => {
                mcDevicePresets = d.presets || {};
                const brandSel = document.getElementById('mcBrand');
                Object.keys(mcDevicePresets).sort().forEach(brand => brandSel.add(new Option(brand, brand)));
            }).catch(() => {});

            document.getElementById('mcBrand').addEventListener('change', () => {
                const brand = document.getElementById('mcBrand').value;
                const modelSel = document.getElementById('mcModel');
                modelSel.innerHTML = '';
                const models = brand ? Object.keys(mcDevicePresets[brand] || {}).sort() : [];
                if (!models.length) {
                    modelSel.add(new Option('- pilih brand dulu -', ''));
                    modelSel.disabled = true;
                    return;
                }
                modelSel.disabled = false;
                models.forEach(m => modelSel.add(new Option(m, m)));
            });

            document.getElementById('mcRandomDevice').addEventListener('change', () => {
                const random = document.getElementById('mcRandomDevice').checked;
                document.getElementById('mcBrand').disabled = random;
                document.getElementById('mcModel').disabled = random || !document.getElementById('mcBrand').value;
            });

            document.getElementById('btnMcPickOutput').addEventListener('click', async () => {
                if (window.pywebview && window.pywebview.api && window.pywebview.api.pick_folder) {
                    try {
                        const path = await window.pywebview.api.pick_folder();
                        if (path) document.getElementById('mcOutputDir').value = path;
                    } catch (e) { showToast('Gagal membuka dialog folder', 'error'); }
                } else {
                    document.getElementById('mcOutputDir').focus();
                    showToast('Ketik path folder secara manual di mode browser', 'info');
                }
            });

            document.getElementById('btnMcAddFiles').addEventListener('click', async () => {
                if (!(window.pywebview && window.pywebview.api && window.pywebview.api.pick_files)) {
                    showToast('Pilih file butuh mode desktop (pywebview)', 'info');
                    return;
                }
                try {
                    const files = await window.pywebview.api.pick_files('media');
                    const added = mcAddPaths(files);
                    if (added) showToast(`${added} file ditambahkan`, 'success');
                } catch (e) { showToast('Gagal membuka dialog file', 'error'); }
            });

            document.getElementById('btnMcAddFolder').addEventListener('click', async () => {
                if (!(window.pywebview && window.pywebview.api && window.pywebview.api.pick_folder)) {
                    showToast('Pilih folder butuh mode desktop (pywebview)', 'info');
                    return;
                }
                try {
                    const folder = await window.pywebview.api.pick_folder();
                    if (!folder) return;
                    const d = await fetch('/api/metadata/scan_folder', {method:'POST', headers:{'Content-Type':'application/json'},
                        body: JSON.stringify({folder})}).then(r => r.json());
                    if (d.error) { showToast(d.error, 'error'); return; }
                    const added = mcAddPaths(d.files || []);
                    showToast(added ? `${added} file ditambahkan dari folder` : 'Tidak ada file media di folder itu', added ? 'success' : 'info');
                } catch (e) { showToast('Gagal membuka dialog folder', 'error'); }
            });

            document.getElementById('btnMcClearQueue').addEventListener('click', () => {
                mcQueue = [];
                renderMcQueue();
            });

            document.getElementById('btnMcStart').addEventListener('click', () => {
                if (!mcQueue.length) { showToast('Antrean masih kosong', 'error'); return; }
                const outputDir = document.getElementById('mcOutputDir').value.trim();
                if (!outputDir) { showToast('Pilih folder output dulu', 'error'); return; }

                const options = {
                    clean: document.getElementById('mcClean').checked,
                    randomize: document.getElementById('mcRandomize').checked,
                    randomize_date: document.getElementById('mcRandomizeDate').checked,
                    rename: document.getElementById('mcRename').checked,
                    tiktok_hd: document.getElementById('mcTiktokHd').checked,
                    upscale: document.getElementById('mcUpscale').checked,
                    aspect_ratio: document.getElementById('mcAspectRatio').value,
                    random_device: document.getElementById('mcRandomDevice').checked,
                };
                const brand = document.getElementById('mcBrand').value;
                const model = document.getElementById('mcModel').value;
                if (!options.random_device && brand && model) options.device = { brand, model };

                const btn = document.getElementById('btnMcStart');
                btn.disabled = true;
                revealProgressPanel('mcProgress');

                api('/api/metadata/clean', 'POST', {
                    files: mcQueue.map(f => f.path),
                    output_dir: outputDir,
                    options,
                }).then(d => {
                    if (!d.job_id) { showToast(d.error || 'Gagal memulai proses', 'error'); btn.disabled = false; return; }
                    const poll = setInterval(() => {
                        fetch(`/api/youtube/job/${d.job_id}`).then(r => r.json()).then(s => {
                            document.getElementById('mcProgPct').textContent = (s.pct || 0) + '%';
                            document.getElementById('mcProgFill').style.width = (s.pct || 0) + '%';
                            document.getElementById('mcProgMsg').textContent = s.message || '';
                            if (Array.isArray(s.rows)) {
                                s.rows.forEach((row, i) => { if (mcQueue[i]) mcQueue[i].status = row.status; });
                                renderMcQueue();
                            }
                            if (!s.done) return;
                            clearInterval(poll);
                            btn.disabled = false;
                            showToast(s.message || (s.success ? 'Selesai' : s.error || 'Gagal'), s.success ? 'success' : 'error');
                        }).catch(() => { clearInterval(poll); btn.disabled = false; });
                    }, 1500);
                }).catch(() => { showToast('Gagal terhubung ke server', 'error'); btn.disabled = false; });
            });
        }
    });
    