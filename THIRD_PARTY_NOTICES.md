# Third-party notices

## Sherlock Project — site registry and checking conventions

FIELDKIT bundles a modified copy of Sherlock's public site registry from [`sherlock-project/sherlock`](https://github.com/sherlock-project/sherlock), specifically [`sherlock_project/resources/data.json`](https://github.com/sherlock-project/sherlock/blob/c588d7c8b476df9f9c84912a05d1430fb8cdcdae/sherlock_project/resources/data.json) at commit `c588d7c8b476df9f9c84912a05d1430fb8cdcdae`. The bundled `sherlock_sites.json` retains 462 safe-for-work site definitions and omits entries marked NSFW. The application adds its own curated site checks and implements a compatible subset of the manifest's `status_code`, `message`, and `response_url` rules; Sherlock's Python application code is not bundled.

Sherlock is distributed under the MIT License. Its copyright and license notice are reproduced below:

```text
MIT License

Copyright (c) 2019 Sherlock Project

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```
