/* Telegram Mini App sharing helper */

function getTelegramWebApp() {
    if (window.Telegram && window.Telegram.WebApp) {
        return window.Telegram.WebApp;
    }
    return null;
}

function getTelegramUser() {
    const tg = getTelegramWebApp();
    if (tg && tg.initDataUnsafe && tg.initDataUnsafe.user) {
        return tg.initDataUnsafe.user;
    }
    return null;
}

function resolveUserId() {
    if (window.State && window.State.userId) {
        return window.State.userId;
    }
    const tgUser = getTelegramUser();
    return tgUser ? tgUser.id : null;
}

async function shareShoppingList(listId, listData) {
    const userId = resolveUserId();
    if (!userId) {
        throw new Error("Missing Telegram user id");
    }

    const tgUser = getTelegramUser();

    const response = await fetch("/api/share", {
        method: "POST",
        headers: {
            "Content-Type": "application/json"
        },
        body: JSON.stringify({
            user_id: userId,
            list_id: listId || "new",
            list_data: listData || null,
            telegram_user: tgUser || null
        })
    });

    if (!response.ok) {
        throw new Error(`Share request failed: ${response.status}`);
    }

    const data = await response.json();
    if (!data || !data.payload) {
        throw new Error("Share payload missing");
    }

    const payload = data.payload;
    const deepLink = `https://t.me/BozorlikAI_bot?start=share_${userId}_${payload}`;

    try {
        if (navigator.clipboard && navigator.clipboard.writeText) {
            await navigator.clipboard.writeText(deepLink);
        }
    } catch (error) {
        console.warn("Clipboard copy failed", error);
    }

    const tg = getTelegramWebApp();
    if (tg && typeof tg.openTelegramLink === "function") {
        tg.openTelegramLink(deepLink);
    } else {
        window.open(deepLink, "_blank", "noopener,noreferrer");
    }

    return deepLink;
}

window.shareShoppingList = shareShoppingList;
