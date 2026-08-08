// FeedEcho — client-side interactions

// Store original row HTML for cancelEdit; avoids XSS from inline HTML serialization
const editState = new Map();

async function testAccount(accountId) {
    try {
        const resp = await fetch(`/api/accounts/${accountId}/test`, { method: 'POST' });
        const data = await resp.json();
        alert(data.message || (data.success ? 'OK' : 'Failed'));
    } catch (e) {
        alert('Request failed: ' + e.message);
    }
}

async function testFeed(feedId) {
    try {
        const resp = await fetch(`/api/feeds/${feedId}/test`, { method: 'POST' });
        const data = await resp.json();
        if (data.success) {
            const p = data.preview;
            alert(`Feed: ${p.title}\nType: ${p.type}\nItems: ${p.item_count}\n\nLatest: ${p.items[0]?.title || 'none'}`);
        } else {
            alert('Feed test failed: ' + data.error);
        }
    } catch (e) {
        alert('Request failed: ' + e.message);
    }
}

async function initFeed(feedId) {
    if (!confirm('Initialize feed? This sets the last seen item so only new posts going forward will be cross-posted.')) return;
    try {
        const resp = await fetch(`/api/feeds/${feedId}/init`, { method: 'POST' });
        const data = await resp.json();
        alert(data.message || (data.success ? 'OK' : 'Failed'));
    } catch (e) {
        alert('Request failed: ' + e.message);
    }
}

async function fetchNow(feedId) {
    try {
        const resp = await fetch(`/api/feeds/${feedId}/fetch`, { method: 'POST' });
        const data = await resp.json();
        alert(data.message || (data.success ? 'OK' : 'Failed'));
    } catch (e) {
        alert('Request failed: ' + e.message);
    }
}

async function toggleEcho(echoId) {
    try {
        const resp = await fetch(`/api/echoes/${echoId}/toggle`, { method: 'POST' });
        const data = await resp.json();
        if (data.success) {
            location.reload();
        }
    } catch (e) {
        alert('Request failed: ' + e.message);
    }
}

function editEcho(echoId) {
    const row = document.getElementById(`echo-row-${echoId}`);
    if (!row) return;

    // Store original HTML to restore on cancel (in memory, not in DOM attribute)
    const originalHTML = row.innerHTML;
    editState.set(echoId, originalHTML);

    const feedId = row.dataset.feedId;
    const destType = row.dataset.destinationType;
    const destId = row.dataset.destinationId;
    const template = row.dataset.template;
    const visibility = row.dataset.visibility;
    const enabled = row.dataset.enabled === '1';

    const feedOpts = document.getElementById('feed-options').innerHTML;
    const mastoOpts = document.getElementById('mastodon-options').innerHTML;
    const emailOpts = document.getElementById('email-options').innerHTML;

    const mastoStyle = destType === 'mastodon' ? '' : 'display:none';
    const emailStyle = destType === 'email' ? '' : 'display:none';

    row.innerHTML = `<td colspan="5">
        <form method="post" action="/api/echoes/${echoId}/edit" class="echo-edit-form">
            <div class="form-row">
                <label>Feed
                    <select name="feed_id" required>${feedOpts}</select>
                </label>
                <label>Destination
                    <select name="destination_type" id="edit-dest-type-${echoId}" onchange="toggleEditDest(${echoId})">
                        ${mastoOpts ? '<option value="mastodon"' + (destType === 'mastodon' ? ' selected' : '') + '>Mastodon Account</option>' : ''}
                        ${emailOpts ? '<option value="email"' + (destType === 'email' ? ' selected' : '') + '>Email Address</option>' : ''}
                    </select>
                </label>
            </div>
            <div class="form-row" id="edit-mastodon-fields-${echoId}" style="${mastoStyle}">
                <label>Mastodon Account
                    <select name="account_id">${mastoOpts}</select>
                </label>
                <label>Visibility
                    <select name="visibility">
                        <option value="public"${visibility === 'public' ? ' selected' : ''}>Public</option>
                        <option value="unlisted"${visibility === 'unlisted' ? ' selected' : ''}>Unlisted</option>
                        <option value="private"${visibility === 'private' ? ' selected' : ''}>Private (followers only)</option>
                        <option value="direct"${visibility === 'direct' ? ' selected' : ''}>Direct</option>
                    </select>
                </label>
            </div>
            <div class="form-row" id="edit-email-fields-${echoId}" style="${emailStyle}">
                <label>Email Address
                    <select name="email_account_id">${emailOpts}</select>
                </label>
            </div>
            <div class="form-row">
                <label>Enabled
                    <input type="checkbox" name="enabled" value="true"${enabled ? ' checked' : ''}>
                </label>
            </div>
            <div class="form-row">
                <label>Template
                    <textarea name="template" rows="3">${escapeHTML(template)}</textarea>
                </label>
            </div>
            <div class="form-row edit-actions">
                <button type="submit" class="btn-sm">Save</button>
                <button type="button" class="btn-sm btn-danger" onclick="cancelEdit(${echoId})">Cancel</button>
            </div>
        </form>
    </td>`;

    // Set selected values in dropdowns
    const feedSelect = row.querySelector('select[name="feed_id"]');
    if (feedSelect) feedSelect.value = feedId;
    const mastoSelect = row.querySelector('select[name="account_id"]');
    if (mastoSelect) mastoSelect.value = destId;
    const emailSelect = row.querySelector('select[name="email_account_id"]');
    if (emailSelect) emailSelect.value = destId;
}

function toggleEditDest(echoId) {
    const destType = document.getElementById(`edit-dest-type-${echoId}`).value;
    document.getElementById(`edit-mastodon-fields-${echoId}`).style.display = destType === 'mastodon' ? '' : 'none';
    document.getElementById(`edit-email-fields-${echoId}`).style.display = destType === 'email' ? '' : 'none';
}

function cancelEdit(echoId) {
    const row = document.getElementById(`echo-row-${echoId}`);
    if (row && editState.has(echoId)) {
        row.innerHTML = editState.get(echoId);
        editState.delete(echoId);
    }
}

function escapeHTML(str) {
    const div = document.createElement('div');
    div.appendChild(document.createTextNode(str));
    return div.innerHTML;
}
