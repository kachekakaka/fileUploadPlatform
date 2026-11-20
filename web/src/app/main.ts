import { createApp } from 'vue'
import { createRouter, createWebHistory } from 'vue-router'
import { ElButton } from 'element-plus'
import 'element-plus/es/components/button/style/css'
import App from './App.vue'
import './styles.css'
import { startEmbed } from '../embed/protocol'

const overview = () => import('../features/overview/Overview.vue')
const accounts = () => import('../features/accounts/Accounts.vue')
const works = () => import('../features/works/Works.vue')
const work = () => import('../features/works/WorkDetail.vue')
const tasks = () => import('../features/tasks/Tasks.vue')
const task = () => import('../features/tasks/TaskDetail.vue')
const schedules = () => import('../features/schedules/Schedules.vue')
const settings = () => import('../features/settings/Settings.vue')

const router = createRouter({
  history: createWebHistory(),
  routes: [
    { path: '/', component: overview, alias: '/overview' },
    { path: '/accounts', component: accounts },
    { path: '/works', component: works },
    { path: '/works/:id', component: work },
    { path: '/tasks', component: tasks },
    { path: '/tasks/:id', component: task },
    { path: '/schedules', component: schedules },
    { path: '/settings', component: settings },
    { path: '/embed/accounts', component: accounts },
    { path: '/embed/works', component: works },
    { path: '/embed/works/:id', component: work },
    { path: '/embed/tasks', component: tasks },
    { path: '/embed/tasks/:id', component: task },
  ],
  scrollBehavior: () => ({ top: 0 }),
})
createApp(App).component('ElButton', ElButton).use(router).mount('#app')
void startEmbed(router)
