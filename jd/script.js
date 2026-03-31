
document.addEventListener('DOMContentLoaded', function() {
    const searchInput = document.getElementById('search-input');
    const toolCards = document.querySelectorAll('.tool-card');
    const themeToggle = document.getElementById('theme-toggle');
    const body = document.body;

    // --- 1. 全局模糊搜索和高亮功能 (保持不变) ---
    function performSearch() {
        const query = searchInput.value.toLowerCase().trim();

        toolCards.forEach(card => {
            // 恢复卡片原样
            card.style.display = 'flex';
            
            // 移除旧的高亮
            let titleElement = card.querySelector('.card-title');
            let subtitleElement = card.querySelector('.card-subtitle');

            // 恢复原始标题和副标题内容
            let originalTitle = card.getAttribute('data-original-title');
            if (originalTitle) titleElement.innerHTML = originalTitle;
            
            let originalSubtitle = card.getAttribute('data-original-subtitle');
            if (originalSubtitle) subtitleElement.innerHTML = originalSubtitle;


            // 获取用于搜索的全部文本（data-label + 标题/副标题）
            const searchableText = (card.getAttribute('data-label') || '') + 
                                   (titleElement ? titleElement.textContent : '') + 
                                   (subtitleElement ? subtitleElement.textContent : '');

            // 如果没有查询内容，则显示所有卡片并跳过高亮
            if (!query) {
                return;
            }

            // --- 执行搜索和高亮 ---
            if (searchableText.toLowerCase().includes(query)) {
                // 卡片匹配，进行高亮处理
                
                // 1. 高亮标题
                if (!card.getAttribute('data-original-title')) {
                    card.setAttribute('data-original-title', titleElement.innerHTML);
                }
                titleElement.innerHTML = highlightMatch(titleElement.textContent, query);
                
                // 2. 高亮副标题
                if (subtitleElement && subtitleElement.textContent.trim() !== '') {
                    if (!card.getAttribute('data-original-subtitle')) {
                        card.setAttribute('data-original-subtitle', subtitleElement.innerHTML);
                    }
                    subtitleElement.innerHTML = highlightMatch(subtitleElement.textContent, query);
                }

            } else {
                // 卡片不匹配，隐藏
                card.style.display = 'none';
            }
        });
    }

    // 高亮匹配文字的函数
    function highlightMatch(text, query) {
        if (!query) return text;
        // 使用正则表达式进行全局替换，并忽略大小写
        const regex = new RegExp(`(${query})`, 'gi');
        return text.replace(regex, '<mark>$1</mark>');
    }

    searchInput.addEventListener('input', performSearch);


    // --- 2. 快捷键功能 (保持不变) ---
    document.addEventListener('keydown', function(e) {
        // 检查 Ctrl 或 Cmd + K
        if ((e.ctrlKey || e.metaKey) && e.key === 'k') {
            e.preventDefault(); // 阻止浏览器默认行为
            searchInput.focus();
            searchInput.select(); // 选中内容方便输入
        }
        
        // 按下 Esc 键清除搜索并失焦
        if (e.key === 'Escape' && document.activeElement === searchInput) {
            searchInput.value = '';
            performSearch();
            searchInput.blur();
        }
    });


    // --- 3. 主题切换功能 (Dark/Light Mode) ---
    function loadTheme() {
        const savedTheme = localStorage.getItem('theme') || 'dark';

        // 默认是深色模式，只有在明确存储为 light 时才进入浅色模式
        if (savedTheme === 'light') {
            body.classList.remove('dark-mode');
            body.classList.add('light-mode'); // 显式添加 light-mode 类
            themeToggle.querySelector('i').className = 'fas fa-moon';
        } else {
            body.classList.remove('light-mode'); // 确保没有 light-mode 类
            body.classList.add('dark-mode');
            themeToggle.querySelector('i').className = 'fas fa-sun';
        }
    }

    function toggleTheme() {
        if (body.classList.contains('dark-mode')) {
            // 切换到浅色模式
            body.classList.remove('dark-mode');
            body.classList.add('light-mode'); // 关键：添加 light-mode 类
            localStorage.setItem('theme', 'light');
            themeToggle.querySelector('i').className = 'fas fa-moon';
        } else {
            // 切换到深色模式
            body.classList.remove('light-mode'); // 关键：移除 light-mode 类
            body.classList.add('dark-mode');
            localStorage.setItem('theme', 'dark');
            themeToggle.querySelector('i').className = 'fas fa-sun';
        }
    }

    themeToggle.addEventListener('click', toggleTheme);
    loadTheme(); // 页面加载时执行

    // --- 4. 侧边导航目录自动生成 (保持不变) ---
    const tocList = document.getElementById('toc-list');
    const categories = document.querySelectorAll('.category-block');

    categories.forEach(category => {
        const titleElement = category.querySelector('.category-title');
        const titleText = titleElement.textContent.trim();
        const categoryId = category.id;

        const listItem = document.createElement('li');
        const link = document.createElement('a');
        
        link.href = `#${categoryId}`;
        link.textContent = titleText.replace(/[\uE000-\uF8FF]|\uD83C[\uDC00-\uDFFF]|\uD83D[\uDC00-\uDFFF]|[\u2011-\u26FF]|\uD83E[\uDC00-\uDFFF]/g, '').trim(); // 移除图标后的文字

        link.addEventListener('click', function(e) {
            e.preventDefault();
            // 使用平滑滚动到分类顶部
            category.scrollIntoView({ behavior: 'smooth' });
        });

        listItem.appendChild(link);
        tocList.appendChild(listItem);
    });
});
