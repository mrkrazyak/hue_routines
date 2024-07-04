from holidays.countries import US


class CustomHolidays(US):
    def _populate(self, year):
        super()._populate(year)
        self._add_holiday_feb_14("Valentine's Day")
        self._add_holiday_mar_17("Saint Patrick's Day")
        self._add_holiday_apr_1("April Fool's Day")
        self._add_holiday_apr_22("Earth Day")
        self._add_holiday_may_5("Cinco de Mayo")
        self._add_holiday_oct_31("Halloween")
        self._add_holiday_dec_24("Christmas Eve")
        self._add_holiday_dec_31("New Year's Eve")
