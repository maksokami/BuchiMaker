import './echarts-theme.js';
import TileWidget from './TileWidget.js';
import BarChartWidget from './BarChartWidget.js';
import HorizontalBarChartWidget from './HorizontalBarChartWidget.js';
import StackedBarChartWidget from './StackedBarChartWidget.js';
import AreaChartWidget from './AreaChartWidget.js';
import PieChartWidget from './PieChartWidget.js';
import RadialGaugeFullWidget from './RadialGaugeFullWidget.js';
import RadialGaugeSemiWidget from './RadialGaugeSemiWidget.js';
import SankeyWidget from './SankeyWidget.js';
import BasicTableWidget from './BasicTableWidget.js';
import InputFilterWidget from './InputFilterWidget.js';
import ButtonFilterWidget from './ButtonFilterWidget.js';
import ButtonUrlWidget from './ButtonUrlWidget.js';
import DropdownMultyWidget from './DropdownMultyWidget.js';
import DropdownSingleWidget from './DropdownSingleWidget.js';
import WidgetStartEndTimeWidget from './WidgetStartEndTimeWidget.js';
import ButtonDatetimeFilterWidget from './ButtonDatetimeFilterWidget.js';

// Inject the package's stylesheet once.
const link = document.createElement('link');
link.rel = 'stylesheet';
link.href = new URL('./theme.css', import.meta.url).href;
document.head.appendChild(link);

export default {
    packageName: 'default',
    components: {
        tile: TileWidget,
        bar_chart: BarChartWidget,
        horizontal_bar_chart: HorizontalBarChartWidget,
        stacked_bar_chart: StackedBarChartWidget,
        area_chart: AreaChartWidget,
        pie_chart: PieChartWidget,
        radial_gauge_full: RadialGaugeFullWidget,
        radial_gauge_semi: RadialGaugeSemiWidget,
        sankey: SankeyWidget,
        basic_table: BasicTableWidget,
        input_filter: InputFilterWidget,
        button_filter: ButtonFilterWidget,
        button_url: ButtonUrlWidget,
        dropdown_multy: DropdownMultyWidget,
        dropdown_single: DropdownSingleWidget,
        widget_start_end_time: WidgetStartEndTimeWidget,
        button_datetime_filter: ButtonDatetimeFilterWidget
    }
};
