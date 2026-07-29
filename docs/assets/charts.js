(function() {
    var style = getComputedStyle(document.documentElement);
    var accent = style.getPropertyValue('--accent').trim();
    var accent2 = style.getPropertyValue('--accent2').trim();
    var accent3 = style.getPropertyValue('--accent3').trim();
    var ink = style.getPropertyValue('--ink').trim();
    var muted = style.getPropertyValue('--muted').trim();
    var rule = style.getPropertyValue('--rule').trim();
    var bg2 = style.getPropertyValue('--bg2').trim();
    var warn = style.getPropertyValue('--warn').trim();
    var danger = style.getPropertyValue('--danger').trim();

    // --- Chart 1: 痛点频次 × 严重程度矩阵（散点图） ---
    var chartMatrix = echarts.init(document.getElementById('chart-matrix'), null, { renderer: 'svg' });
    chartMatrix.setOption({
        animation: false,
        title: { text: '痛点分布矩阵', left: 'center', textStyle: { color: ink, fontSize: 14 } },
        tooltip: {
            appendToBody: true,
            formatter: function(p) {
                return '<b>' + p.data[3] + '</b><br/>频次: ' + p.data[0] + ' 条<br/>严重度: ' + p.data[1].toFixed(1) + '/3';
            }
        },
        grid: { left: 60, right: 30, top: 50, bottom: 60 },
        xAxis: {
            name: '出现频次（条）',
            nameLocation: 'middle',
            nameGap: 35,
            nameTextStyle: { color: muted, fontSize: 12 },
            axisLine: { lineStyle: { color: rule } },
            axisLabel: { color: muted },
            splitLine: { lineStyle: { color: rule, type: 'dashed', opacity: 0.3 } }
        },
        yAxis: {
            name: '平均严重程度 (1-3)',
            nameLocation: 'middle',
            nameGap: 40,
            nameTextStyle: { color: muted, fontSize: 12 },
            min: 1, max: 3,
            axisLine: { lineStyle: { color: rule } },
            axisLabel: { color: muted },
            splitLine: { lineStyle: { color: rule, type: 'dashed', opacity: 0.3 } }
        },
        series: [{
            type: 'scatter',
            symbolSize: function(data) { return Math.sqrt(data[0]) * 4 + 10; },
            data: [
                [320, 2.8, '续航不足', '续航不足'],
                [280, 2.5, '屏幕易碎', '屏幕易碎'],
                [210, 2.2, '重量过大', '重量过大'],
                [180, 1.8, '系统卡顿', '系统卡顿'],
                [150, 2.6, '防水失效', '防水失效'],
                [130, 2.9, '充电口损坏', '充电口损坏'],
                [110, 1.5, '相机画质差', '相机画质差'],
                [95, 2.1, '信号弱', '信号弱'],
                [80, 2.4, 'App配对失败', 'App配对失败'],
                [65, 1.7, '按键手感差', '按键手感差'],
                [50, 2.7, 'OTA变砖', 'OTA变砖'],
                [40, 1.9, '扬声器音量小', '扬声器音量小']
            ],
            itemStyle: {
                color: function(p) {
                    if (p.data[1] >= 2.5) return danger;
                    if (p.data[1] >= 2.0) return warn;
                    return accent;
                },
                opacity: 0.75,
                shadowBlur: 10,
                shadowColor: 'rgba(0,0,0,0.3)'
            },
            label: {
                show: true,
                formatter: function(p) { return p.data[3]; },
                position: 'top',
                color: ink,
                fontSize: 11
            }
        }]
    });
    window.addEventListener('resize', function() { chartMatrix.resize(); });

    // --- Chart 2: 竞品痛点维度雷达对比 ---
    var chartRadar = echarts.init(document.getElementById('chart-radar'), null, { renderer: 'svg' });
    chartRadar.setOption({
        animation: false,
        title: { text: '竞品负面评论占比对比', left: 'center', textStyle: { color: ink, fontSize: 14 } },
        tooltip: { appendToBody: true },
        legend: {
            data: ['Blackview', 'Ulefone', 'RugOne'],
            top: 30,
            textStyle: { color: muted }
        },
        radar: {
            indicator: [
                { name: '续航', max: 100 },
                { name: '屏幕', max: 100 },
                { name: '防水', max: 100 },
                { name: '系统', max: 100 },
                { name: '重量', max: 100 },
                { name: '信号', max: 100 }
            ],
            center: ['50%', '58%'],
            radius: '60%',
            axisName: { color: ink, fontSize: 12 },
            splitLine: { lineStyle: { color: rule } },
            splitArea: { areaStyle: { color: ['rgba(0,212,255,0.02)', 'rgba(0,212,255,0.05)'] } },
            axisLine: { lineStyle: { color: rule } }
        },
        series: [{
            type: 'radar',
            data: [
                {
                    value: [75, 68, 45, 55, 70, 40],
                    name: 'Blackview',
                    itemStyle: { color: accent },
                    areaStyle: { color: 'rgba(0,212,255,0.1)' },
                    lineStyle: { color: accent }
                },
                {
                    value: [60, 55, 50, 65, 50, 35],
                    name: 'Ulefone',
                    itemStyle: { color: accent2 },
                    areaStyle: { color: 'rgba(255,107,53,0.1)' },
                    lineStyle: { color: accent2 }
                },
                {
                    value: [45, 40, 30, 35, 55, 25],
                    name: 'RugOne',
                    itemStyle: { color: accent3 },
                    areaStyle: { color: 'rgba(74,222,128,0.1)' },
                    lineStyle: { color: accent3 }
                }
            ]
        }]
    });
    window.addEventListener('resize', function() { chartRadar.resize(); });

    // --- Chart 3: 痛点趋势变化 ---
    var chartTrend = echarts.init(document.getElementById('chart-trend'), null, { renderer: 'svg' });
    var months = ['2月', '3月', '4月', '5月', '6月', '7月'];
    chartTrend.setOption({
        animation: false,
        title: { text: '痛点评论量月度趋势', left: 'center', textStyle: { color: ink, fontSize: 14 } },
        tooltip: { trigger: 'axis', appendToBody: true },
        legend: {
            data: ['续航', '屏幕', '系统', '防水'],
            top: 30,
            textStyle: { color: muted }
        },
        grid: { left: 50, right: 30, top: 70, bottom: 40 },
        xAxis: {
            type: 'category',
            data: months,
            axisLine: { lineStyle: { color: rule } },
            axisLabel: { color: muted }
        },
        yAxis: {
            type: 'value',
            name: '评论数',
            nameTextStyle: { color: muted },
            axisLine: { lineStyle: { color: rule } },
            axisLabel: { color: muted },
            splitLine: { lineStyle: { color: rule, type: 'dashed', opacity: 0.3 } }
        },
        series: [
            {
                name: '续航',
                type: 'line',
                smooth: true,
                data: [180, 220, 280, 320, 290, 310],
                itemStyle: { color: danger },
                lineStyle: { color: danger, width: 2 }
            },
            {
                name: '屏幕',
                type: 'line',
                smooth: true,
                data: [150, 180, 210, 250, 280, 260],
                itemStyle: { color: warn },
                lineStyle: { color: warn, width: 2 }
            },
            {
                name: '系统',
                type: 'line',
                smooth: true,
                data: [90, 110, 130, 160, 180, 150],
                itemStyle: { color: accent },
                lineStyle: { color: accent, width: 2 }
            },
            {
                name: '防水',
                type: 'line',
                smooth: true,
                data: [60, 80, 100, 130, 150, 140],
                itemStyle: { color: accent3 },
                lineStyle: { color: accent3, width: 2 }
            }
        ]
    });
    window.addEventListener('resize', function() { chartTrend.resize(); });

    // --- Chart 4: MVP 月度成本结构 ---
    var chartCost = echarts.init(document.getElementById('chart-cost'), null, { renderer: 'svg' });
    chartCost.setOption({
        animation: false,
        title: { text: 'MVP 月度成本结构（预估 $600）', left: 'center', textStyle: { color: ink, fontSize: 14 } },
        tooltip: {
            trigger: 'item',
            appendToBody: true,
            formatter: '{b}: ${c} ({d}%)'
        },
        legend: {
            orient: 'vertical',
            left: 'left',
            top: 'middle',
            textStyle: { color: muted, fontSize: 12 }
        },
        series: [{
            type: 'pie',
            radius: ['40%', '70%'],
            center: ['60%', '55%'],
            avoidLabelOverlap: false,
            itemStyle: { borderColor: bg2, borderWidth: 2 },
            label: {
                show: true,
                formatter: '${c}',
                color: ink,
                fontSize: 13,
                fontWeight: 'bold'
            },
            data: [
                { value: 350, name: '数据抓取\n(Bright Data+Apify)', itemStyle: { color: accent } },
                { value: 100, name: 'LLM 分析\n(Gemini+Claude)', itemStyle: { color: accent2 } },
                { value: 50, name: 'Qdrant 自托管', itemStyle: { color: accent3 } },
                { value: 50, name: 'PostgreSQL', itemStyle: { color: warn } },
                { value: 50, name: 'GCP Cloud Run', itemStyle: { color: danger } }
            ]
        }]
    });
    window.addEventListener('resize', function() { chartCost.resize(); });

})();
