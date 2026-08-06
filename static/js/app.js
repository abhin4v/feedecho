// FeedEcho — client-side interactions

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
