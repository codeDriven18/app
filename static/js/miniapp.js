// Chatbot functionality
const chatbotButton = document.getElementById('chatbotButton');
const chatbotWindow = document.getElementById('chatbotWindow');
const chatbotClose = document.getElementById('chatbotClose');
const chatbotInput = document.getElementById('chatbotInput');
const chatbotSend = document.getElementById('chatbotSend');
const chatbotMessages = document.getElementById('chatbotMessages');

if (chatbotButton && chatbotWindow) {
    chatbotButton.addEventListener('click', () => {
        chatbotWindow.classList.toggle('active');
    });
}

if (chatbotClose && chatbotWindow) {
    chatbotClose.addEventListener('click', () => {
        chatbotWindow.classList.remove('active');
    });
}

async function sendMessage() {
    if (!chatbotInput || !chatbotMessages) {
        return;
    }

    const message = chatbotInput.value.trim();
    if (!message) {
        return;
    }

    // Add user message to chat
    const userMessageDiv = document.createElement('div');
    userMessageDiv.className = 'chat-message user';
    userMessageDiv.innerHTML = `<div class="message-bubble">${message}</div>`;
    chatbotMessages.appendChild(userMessageDiv);
    chatbotInput.value = '';

    // Add typing indicator
    const typingDiv = document.createElement('div');
    typingDiv.className = 'chat-message bot';
    typingDiv.innerHTML = `
        <div class="typing-indicator active">
            <div class="typing-dots">
                <span></span>
                <span></span>
                <span></span>
            </div>
        </div>
    `;
    chatbotMessages.appendChild(typingDiv);
    chatbotMessages.scrollTop = chatbotMessages.scrollHeight;

    // Send to backend
    try {
        const response = await fetch('http://localhost:5000/chat', {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json',
            },
            body: JSON.stringify({ message: message })
        });

        if (!response.ok) {
            throw new Error(`HTTP error! status: ${response.status}`);
        }

        const data = await response.json();

        // Remove typing indicator
        typingDiv.remove();

        // Check if response exists
        if (data.response) {
            // Add bot response
            const botMessageDiv = document.createElement('div');
            botMessageDiv.className = 'chat-message bot';
            botMessageDiv.innerHTML = `<div class="message-bubble">${data.response}</div>`;
            chatbotMessages.appendChild(botMessageDiv);
            chatbotMessages.scrollTop = chatbotMessages.scrollHeight;
        } else if (data.error) {
            throw new Error(data.error);
        } else {
            throw new Error('No response from server');
        }
    } catch (error) {
        console.error('Error:', error);
        typingDiv.remove();
        const errorDiv = document.createElement('div');
        errorDiv.className = 'chat-message bot';
        let errorMessage = 'Kechirasiz, xatolik yuz berdi.';
        if (error.message && error.message.includes('Failed to fetch')) {
            errorMessage = 'Server bilan aloqa yo\'q. Backend ishga tushganligini tekshiring.';
        } else if (error.message) {
            errorMessage = `Xatolik: ${error.message}`;
        }
        errorDiv.innerHTML = `<div class="message-bubble">${errorMessage}</div>`;
        chatbotMessages.appendChild(errorDiv);
        chatbotMessages.scrollTop = chatbotMessages.scrollHeight;
    }
}

if (chatbotSend) {
    chatbotSend.addEventListener('click', sendMessage);
}

if (chatbotInput) {
    chatbotInput.addEventListener('keypress', (e) => {
        if (e.key === 'Enter') {
            sendMessage();
        }
    });
}

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
        throw new Error('Missing Telegram user id');
    }

    const tgUser = getTelegramUser();

    const response = await fetch('/api/share', {
        method: 'POST',
        headers: {
            'Content-Type': 'application/json'
        },
        body: JSON.stringify({
            user_id: userId,
            list_id: listId || 'new',
            list_data: listData || null,
            telegram_user: tgUser || null
        })
    });

    if (!response.ok) {
        throw new Error(`Share request failed: ${response.status}`);
    }

    const data = await response.json();
    if (!data || !data.payload) {
        throw new Error('Share payload missing');
    }

    const payload = data.payload;
    const deepLink = `https://t.me/BozorlikAI_bot?start=share_${userId}_${payload}`;

    try {
        if (navigator.clipboard && navigator.clipboard.writeText) {
            await navigator.clipboard.writeText(deepLink);
        }
    } catch (error) {
        console.warn('Clipboard copy failed', error);
    }

    const tg = getTelegramWebApp();
    if (tg && typeof tg.openTelegramLink === 'function') {
        tg.openTelegramLink(deepLink);
    } else {
        window.open(deepLink, '_blank', 'noopener,noreferrer');
    }

    return deepLink;
}

window.shareShoppingList = shareShoppingList;
